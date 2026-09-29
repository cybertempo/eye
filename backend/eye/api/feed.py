"""Bounded, database-backed wire messages for the browser API (Package 3).

Everything here reads the database and returns ``eye.wire/3`` message dicts;
nothing writes. Callers pass a connection opened by ``open_reader`` (read-only
session, statement timeout). Every query is bounded by area, interval, layer
and row limits; a request that would exceed a limit is refused with
``QueryRefused`` rather than silently truncated.

Cursors are ``e<epoch>:<change_seq>``: the database's random feed epoch
(migration 0003) and a position in ``eye.feed_change``. A cursor from another
or a rebuilt database names a different epoch and is reported as expired.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eye.api import events as event_ledger
from eye.ingest.capture import APPROVED_SOURCES, EVENT_CAPTURE_FORMAT
from eye.storage.db import Connection, connect
from eye.wire import SCHEMA_VERSION

LAYERS = ("flight", "vessel", "road")
SYNTHETIC_SOURCES = APPROVED_SOURCES  # every approved source is invented data today
SOURCE_LABELS = {
    "synthetic-ais": "Synthetic AIS (invented vessels; not a real AIS feed)",
    "synthetic-fixture": "Synthetic fixture (invented records)",
    "synthetic-events": "Synthetic event reports (invented cases; not a real authority)",
}
SYNTHETIC_NOTICE = (
    "Invented data for the public demo. No record describes a real aircraft, vessel, road or event."
)
CURSOR = re.compile(
    r"^e([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([0-9]{1,18})$"
)
MAX_TRACK_POINTS = 1000  # wire schema Track.points maxItems
MAX_TRACK_CONFLICTS = 100  # wire schema Track.conflicts maxItems
MAX_CONFLICT_CLAIMS = 20  # wire schema PositionConflict.claims maxItems
MAX_CLAIM_EVIDENCE = 100  # wire schema ConflictClaim.evidence_batch_ids maxItems
DELTA_MAX_ITEMS = 500  # wire schema DeltaMessage *_upserted maxItems
MAX_FLAGS = 16


class QueryRefused(ValueError):
    """The request is outside the configured bounds. ``status`` is the HTTP status."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


@dataclass(frozen=True)
class Limits:
    max_interval_hours: int
    max_tracks: int
    max_points: int
    max_coverage: int
    max_counts: int
    max_crossings: int
    max_events: int
    max_changes: int
    query_timeout_ms: int
    default_view_hours: int


@dataclass(frozen=True)
class Query:
    bbox: tuple[float, float, float, float]
    start: datetime
    end: datetime
    layers: tuple[str, ...]


# --- connections -------------------------------------------------------------------


def open_reader(url: str, *, require_loopback: bool, query_timeout_ms: int) -> Connection:
    """A read-only session: the API can never write ingest history."""
    conn = connect(url, require_loopback=require_loopback)
    conn.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.run(f"SET statement_timeout = {int(query_timeout_ms)}")
    return conn


def _begin(conn: Connection) -> None:
    # One consistent view: the cursor and the rows it describes come from the
    # same snapshot.
    conn.run("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")


def _end(conn: Connection) -> None:
    conn.run("COMMIT")


# --- time, cursors and query parsing -------------------------------------------------


def iso(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}".rstrip("0")
    return text + "Z"


def parse_time(text: str, what: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z", text):
        raise QueryRefused(400, f"{what} must be an RFC 3339 UTC time ending in Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise QueryRefused(400, f"{what} is not a real calendar time") from exc


def iso_parse(text: str) -> datetime:
    """A stored wire time (already valid) as a datetime."""
    return datetime.fromisoformat(text)


def make_cursor(epoch: str, seq: int) -> str:
    return f"e{epoch}:{seq}"


def parse_cursor(text: str | None) -> tuple[str, int] | None:
    if not text:
        return None
    match = CURSOR.fullmatch(text)
    return (match.group(1), int(match.group(2))) if match else None


def check_query(query: Query, limits: Limits) -> Query:
    west, south, east, north = query.bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise QueryRefused(
            400, "bbox must be west,south,east,north with west < east, south < north"
        )
    if query.end <= query.start:
        raise QueryRefused(400, "interval end must be after its start")
    if query.end - query.start > timedelta(hours=limits.max_interval_hours):
        raise QueryRefused(
            413, f"interval longer than {limits.max_interval_hours} hours; narrow the request"
        )
    if not query.layers or any(layer not in LAYERS for layer in query.layers):
        raise QueryRefused(400, f"layers must be a non-empty subset of {', '.join(LAYERS)}")
    return query


def query_from_params(params: dict[str, list[str]], default: Query, limits: Limits) -> Query:
    """Build a query from URL parameters; absent parameters take the default view."""
    for name in params:
        if name not in ("bbox", "start", "end", "layers", "line"):
            raise QueryRefused(400, f"unknown query parameter {name!r}")
        if len(params[name]) != 1:
            raise QueryRefused(400, f"query parameter {name!r} must appear once")
    one = {name: values[0] for name, values in params.items()}
    bbox = default.bbox
    if "bbox" in one:
        parts = one["bbox"].split(",")
        try:
            numbers = tuple(float(p) for p in parts)
        except ValueError as exc:
            raise QueryRefused(400, "bbox must be four numbers") from exc
        if len(numbers) != 4 or any(n != n or n in (float("inf"), float("-inf")) for n in numbers):
            raise QueryRefused(400, "bbox must be four finite numbers")
        bbox = numbers  # type: ignore[assignment]
    start = parse_time(one["start"], "start") if "start" in one else default.start
    end = parse_time(one["end"], "end") if "end" in one else default.end
    layers = tuple(one["layers"].split(",")) if "layers" in one else default.layers
    return check_query(Query(bbox, start, end, layers), limits)


def default_query(conn: Connection, bbox, layers, limits: Limits) -> Query:
    """The default view: the latest ``default_view_hours`` that have any coverage."""
    rows = conn.run("SELECT max(interval_end) FROM eye.coverage")
    latest = rows[0][0]
    if latest is None:
        latest = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    latest = latest.astimezone(UTC)
    return Query(tuple(bbox), latest - timedelta(hours=limits.default_view_hours), latest, layers)


def head(conn: Connection) -> tuple[str, int]:
    epoch = conn.run("SELECT epoch::text FROM eye.feed_epoch")[0][0]
    seq = conn.run("SELECT coalesce(max(change_seq), 0) FROM eye.feed_change")[0][0]
    return epoch, int(seq)


# --- snapshot and deltas -------------------------------------------------------------


TRACK_POINTS = """
SELECT o.observation_id::text, o.source_id, o.source_record_id, o.layer, o.display_type::text,
       eye.iso_utc(o.observed_time), eye.iso_utc(v.first_received_time),
       ST_X(o.position), ST_Y(o.position), o.altitude_m, o.quality_flags::text[],
       v.is_current
FROM eye.observation o
JOIN eye.observation_version v USING (observation_id)
WHERE v.is_current IS NOT FALSE
  AND o.layer = ANY(CAST(:layers AS text[]))
  AND o.observed_time >= :start AND o.observed_time < :end
  AND o.position && ST_MakeEnvelope(:w, :s, :e, :n, 4326)
  AND (CAST(:records AS text[]) IS NULL
       OR o.source_id || ' ' || o.layer || ' ' || o.source_record_id
          = ANY(CAST(:records AS text[])))
ORDER BY o.source_id, o.layer, o.source_record_id, o.observed_time, o.source_published_time,
         o.observation_id
LIMIT :cap
"""

# Every claim of a contested observed time, wherever it lies: a conflict is
# shown whole if any of its claims is in view. Each conflict is read up to one
# claim past the wire limit, and each claim up to one batch past it, so a
# conflict too large for one message is recognised and refused by name before
# the response is built, never cut short or left to fail validation.
CONFLICT_CLAIMS = """
SELECT c.observation_id::text, c.source_id, c.layer, c.source_record_id,
       eye.iso_utc(c.observed_time), eye.iso_utc(c.source_published_time),
       eye.iso_utc(c.first_received_time), ST_X(c.position), ST_Y(c.position), c.altitude_m,
       ARRAY(SELECT r.batch_id::text FROM eye.observation_receipt r
             WHERE r.observation_id = c.observation_id
             ORDER BY r.batch_id::text LIMIT :evidence_cap)
FROM (
  SELECT o.*, v.first_received_time,
         row_number() OVER (PARTITION BY o.source_id, o.layer, o.source_record_id,
                                         o.observed_time
                            ORDER BY o.observation_id) AS claim_rank
  FROM eye.observation o
  JOIN eye.observation_version v USING (observation_id)
  WHERE v.is_current IS NULL
    AND o.source_id || ' ' || o.layer || ' ' || o.source_record_id || ' '
        || eye.iso_utc(o.observed_time) = ANY(CAST(:contested AS text[]))
) c
WHERE c.claim_rank <= :claim_cap
ORDER BY c.source_id, c.layer, c.source_record_id, c.observed_time, c.observation_id
LIMIT :cap
"""

COVERAGE = """
SELECT c.coverage_id::text, c.source_id, c.layer, eye.iso_utc(c.interval_start),
       eye.iso_utc(c.interval_end), c.state::text, c.reason, c.metric_name, c.metric_value,
       b.batch_id::text, eye.iso_utc(b.attempt_finished_at),
       ST_Covers(b.requested_area, ST_MakeEnvelope(:w, :s, :e, :n, 4326))
FROM eye.coverage c JOIN eye.capture_batch b USING (batch_id)
WHERE c.layer = ANY(CAST(:layers AS text[]))
  AND ((c.interval_start < :end AND c.interval_end > :start
        AND b.requested_area && ST_MakeEnvelope(:w, :s, :e, :n, 4326))
       OR c.batch_id::text = ANY(CAST(:counted AS text[])))
  AND (CAST(:batch AS uuid) IS NULL OR c.batch_id = CAST(:batch AS uuid))
ORDER BY c.interval_start, c.layer, c.source_id, c.coverage_id
LIMIT :cap
"""


def _params(query: Query) -> dict:
    west, south, east, north = query.bbox
    return {
        "layers": list(query.layers),
        "start": query.start,
        "end": query.end,
        "w": west,
        "s": south,
        "e": east,
        "n": north,
    }


def track_id(source: str, layer: str, record: str) -> str:
    """A stable track id: the same (source, layer, record) always gives the same id.

    A source record is identified by source, layer and record id together, so
    one record id reused in two layers is two tracks. SHA-256 over an
    unambiguous encoding of the three parts, kept to 128 bits: never a
    truncation of the readable name, so two records cannot share an id by
    sharing a prefix.
    """
    digest = hashlib.sha256(json.dumps([source, layer, record]).encode("utf-8")).hexdigest()
    return f"trk-{digest[:32]}"


def _t(text: str) -> str:
    return text.replace(".000000Z", "Z")


def _tracks(conn, query: Query, limits: Limits, records: list[str] | None) -> list[dict]:
    """Tracks keyed by (source, layer, record).

    Resolved (current) positions form the route. An observed time whose latest
    publication is contested contributes no point: all of its claims are kept
    under ``conflicts`` with their evidence, none joined to the route or taken
    as the track's position.
    """
    rows = conn.run(TRACK_POINTS, **_params(query), records=records, cap=limits.max_points + 1)
    if len(rows) > limits.max_points:
        raise QueryRefused(413, f"more than {limits.max_points} positions; narrow the request")
    tracks: dict[tuple[str, str, str], dict] = {}
    contested: list[str] = []
    for (
        _oid,
        source,
        record,
        layer,
        display,
        observed,
        received,
        lon,
        lat,
        alt,
        flags,
        current,
    ) in rows:
        if layer not in ("flight", "vessel"):
            continue  # road observations are not tracks; the event ledger is Package 4c
        key = (source, layer, record)
        track = tracks.get(key)
        if track is None:
            if len(tracks) >= limits.max_tracks:
                raise QueryRefused(413, f"more than {limits.max_tracks} tracks; narrow the request")
            track = tracks[key] = {
                "id": track_id(source, layer, record),
                "kind": layer,
                "source": source,
                "source_record_id": record,
                "display_type": display,
                "points": [],
                "quality_flags": [],
            }
        wanted = list(flags or [])
        if current is None:
            contested.append(f"{source} {layer} {record} {observed}")
            wanted.append("publication_conflict")
        else:
            if len(track["points"]) >= MAX_TRACK_POINTS:
                raise QueryRefused(413, "a track has more positions than one message allows")
            track["points"].append(
                {
                    "observed_time": _t(observed),
                    "received_time": _t(received),
                    "lon": float(lon),
                    "lat": float(lat),
                    "alt_m": None if alt is None else float(alt),
                }
            )
        for flag in wanted:
            if flag not in track["quality_flags"] and len(track["quality_flags"]) < MAX_FLAGS:
                track["quality_flags"].append(flag)
    if contested:
        _add_conflicts(conn, tracks, sorted(set(contested)), limits)
    return list(tracks.values())


def _add_conflicts(conn, tracks: dict, contested: list[str], limits: Limits) -> None:
    row_cap = limits.max_points + 1
    claims = conn.run(
        CONFLICT_CLAIMS,
        contested=contested,
        claim_cap=MAX_CONFLICT_CLAIMS + 1,
        evidence_cap=MAX_CLAIM_EVIDENCE + 1,
        cap=row_cap,
    )
    if len(claims) > limits.max_points:
        raise QueryRefused(413, f"more than {limits.max_points} positions; narrow the request")
    for oid, source, layer, record, observed, published, received, lon, lat, alt, batches in claims:
        track = tracks[(source, layer, record)]
        conflicts = track.setdefault("conflicts", [])
        if not conflicts or conflicts[-1]["observed_time"] != _t(observed):
            if len(conflicts) >= MAX_TRACK_CONFLICTS:
                raise QueryRefused(
                    413,
                    f"{layer} record {record} from {source} has more than "
                    f"{MAX_TRACK_CONFLICTS} contested times in view; one message carries at "
                    f"most {MAX_TRACK_CONFLICTS}. Narrow the interval",
                )
            conflicts.append({"observed_time": _t(observed), "claims": []})
        conflict = conflicts[-1]
        if len(conflict["claims"]) >= MAX_CONFLICT_CLAIMS:
            raise QueryRefused(
                413,
                f"{layer} record {record} from {source} has more than {MAX_CONFLICT_CLAIMS} "
                f"conflicting claims at {conflict['observed_time']}; one message carries at "
                f"most {MAX_CONFLICT_CLAIMS} and none is dropped. Narrow the area or interval "
                "to leave that time out",
            )
        if len(batches) > MAX_CLAIM_EVIDENCE:
            raise QueryRefused(
                413,
                f"claim {oid} has more than {MAX_CLAIM_EVIDENCE} evidence batches; one message "
                f"carries at most {MAX_CLAIM_EVIDENCE}",
            )
        conflict["claims"].append(
            {
                "observation_id": oid,
                "published_time": _t(published),
                "received_time": _t(received),
                "lon": float(lon),
                "lat": float(lat),
                "alt_m": None if alt is None else float(alt),
                "evidence_batch_ids": list(batches),
            }
        )


PART_OF_VIEW = "the source's area covers only part of this view; outside it events are unknown"


def _coverage(
    conn, query: Query, limits: Limits, batch: str | None, events: list[dict] | None = None
) -> tuple[list[dict], set, list[tuple]]:
    """Coverage rows in view, their sources, and the event-report spans whose
    source area covers the whole view (the only ones that can close a gap).

    An event-report row whose source area only overlaps the view cannot speak
    for all of it: a qualified row becomes partial (a lower bound) with the
    reason, so a small healthy source is never read as a measured zero for a
    larger view.

    Event-report rows are served in the view's own area but keep their
    batch's own time: the interval is the capture's reporting window (when
    the reports it holds were published, marked ``interval_kind:
    reporting_window``), never clipped to the view and never an occurrence
    interval. The count is the number of cases in ``events`` (the event list
    for this view) whose current or conflicting latest report came from that
    batch, under the metric ``event_cases_in_view``. A batch whose window lies
    outside the view's interval is still listed when it holds such a report (a
    later report about an earlier event), so every counted case has its row.
    One case can be counted by more than one row (the same claim delivered
    twice), so the rows are never a total. The stored per-batch metric, which
    counts the whole capture, is never shown as a count for the view.
    """
    counted = sorted(event_ledger.reporting_batches(events or []))
    rows = conn.run(
        COVERAGE, **_params(query), batch=batch, counted=counted, cap=limits.max_coverage + 1
    )
    if len(rows) > limits.max_coverage:
        raise QueryRefused(
            413, f"more than {limits.max_coverage} coverage rows; narrow the request"
        )
    out, sources, full = [], set(), []
    view_start, view_end = iso(query.start), iso(query.end)
    for _cid, source, layer, start, end, state, reason, metric, value, bid, _rx, covers in rows:
        if layer not in LAYERS:
            continue
        sources.add(source)
        start, end = _t(start), _t(end)
        interval_kind = None
        if metric == event_ledger.EVENT_METRIC:
            interval_kind = event_ledger.REPORTING_WINDOW
            metric = event_ledger.EVENT_VIEW_METRIC
            if value is not None:
                value = event_ledger.cases_in_view_from(events or [], bid)
            span = event_ledger.clip(start, end, view_start, view_end)
            if covers:
                if event_ledger.before(span[0], span[1]):
                    full.append((layer, *span))
            elif state in ("qualified", "partial"):
                state = "partial"
                reason = PART_OF_VIEW if reason is None else f"{reason}; {PART_OF_VIEW}"[:500]
        item = {
            "layer": layer,
            "interval": {"start": start, "end": end},
            "state": state,
            "metric": {"name": metric, "value": None if value is None else _number(value)},
        }
        if interval_kind is not None:
            item["interval_kind"] = interval_kind
            item["batch_id"] = bid
        if reason is not None:
            item["reason"] = reason
        out.append(item)
    return out, sources, full


def _number(value) -> float | int:
    number = float(value)
    return int(number) if number.is_integer() else number


def _synthetic(conn, sources: set[str]) -> bool:
    """True only if every source behind the result (or, for an empty result,
    every source in the database) is an invented one."""
    if not sources:
        sources = {s for (s,) in conn.run("SELECT DISTINCT source_id FROM eye.capture_batch")}
    return all(source in SYNTHETIC_SOURCES for source in sources)


def _events(conn, query: Query, limits: Limits, keys: list[str] | None) -> list[dict]:
    try:
        return event_ledger.cases(conn, _params(query), limits, keys, SOURCE_LABELS)
    except event_ledger.EventsRefused as exc:
        raise QueryRefused(exc.status, str(exc)) from exc


def snapshot(conn: Connection, query: Query, limits: Limits, area_name: str) -> dict:
    _begin(conn)
    try:
        epoch, seq = head(conn)
        tracks = _tracks(conn, query, limits, None)
        events = _events(conn, query, limits, None)
        coverage, sources, full = _coverage(conn, query, limits, None, events)
        # Where no event-report source covered the whole view, events are unknown.
        coverage += event_ledger.coverage_gaps(full, query.layers, iso(query.start), iso(query.end))
        sources |= {t["source"] for t in tracks} | {e["source"] for e in events}
        synthetic = _synthetic(conn, sources)
    finally:
        _end(conn)
    message = {
        "schema_version": SCHEMA_VERSION,
        "kind": "snapshot",
        "cursor": make_cursor(epoch, seq),
        "generated_at": iso(datetime.now(UTC)),
        "synthetic": synthetic,
        "area": {"name": area_name, "bbox": list(query.bbox)},
        "interval": {"start": iso(query.start), "end": iso(query.end)},
        "tracks": tracks,
        "events": events,
        "coverage": coverage,
    }
    if synthetic:
        message["notice"] = SYNTHETIC_NOTICE
    return message


def changes_after(conn: Connection, seq: int, limit: int) -> list[tuple]:
    """Change rows after ``seq``, oldest first: (seq, kind, batch_id, run_id, layer)."""
    return conn.run(
        "SELECT change_seq, kind, batch_id::text, run_id::text, layer FROM eye.feed_change "
        "WHERE change_seq > :seq ORDER BY change_seq LIMIT :limit",
        seq=seq,
        limit=limit,
    )


def delta(
    conn: Connection,
    query: Query,
    limits: Limits,
    epoch: str,
    change: tuple,
    previous_seq: int,
    sequence: int,
) -> dict:
    """The delta for one change row, restricted to one subscription."""
    seq, kind, batch_id, _run_id, layer = change
    tracks: list[dict] = []
    events: list[dict] = []
    coverage: list[dict] = []
    fmt = None
    if kind == "capture_batch" and layer in query.layers:
        fmt = conn.run(
            "SELECT capture_format FROM eye.capture_batch WHERE batch_id = CAST(:b AS uuid)",
            b=batch_id,
        )[0][0]
    if fmt == EVENT_CAPTURE_FORMAT:
        events = _event_delta(conn, query, limits, batch_id)
        coverage, _, added = _coverage(conn, query, limits, batch_id, events)
        if added:
            _check_gaps_unchanged(conn, query, limits, added)
        _check_counts_unchanged(batch_id, events)
        if len(events) > DELTA_MAX_ITEMS or len(coverage) > DELTA_MAX_ITEMS:
            raise QueryRefused(413, "batch is larger than one delta allows")
    elif kind == "capture_batch" and layer in query.layers:
        limit = min(limits.max_tracks, DELTA_MAX_ITEMS)
        # The records this batch changed that have, or had, any version inside
        # the subscription: a correction can move a record's current point out
        # of view, and the client may still hold the old one. The query asks
        # for one more than the limit: a batch that touches more cannot be sent
        # as one delta, and is refused rather than cut short.
        touched = conn.run(
            "SELECT DISTINCT o.source_id, o.layer, o.source_record_id "
            "FROM eye.observation_receipt r JOIN eye.observation o USING (observation_id) "
            "WHERE r.batch_id = CAST(:batch AS uuid) "
            "AND o.layer = ANY(CAST(:layers AS text[])) "
            "AND EXISTS (SELECT 1 FROM eye.observation v "
            "WHERE v.source_id = o.source_id AND v.source_record_id = o.source_record_id "
            "AND v.layer = o.layer AND v.observed_time >= :start AND v.observed_time < :end "
            "AND v.position && ST_MakeEnvelope(:w, :s, :e, :n, 4326)) "
            "LIMIT :cap",
            batch=batch_id,
            cap=limit + 1,
            **_params(query),
        )
        if len(touched) > limit:
            raise QueryRefused(413, f"batch changes more than {limit} tracks in view")
        if touched:
            tracks = _tracks(conn, query, limits, [f"{s} {lay} {r}" for s, lay, r in touched])
        remaining = {(t["source"], t["kind"], t["source_record_id"]) for t in tracks}
        # Road records are not tracks (the event ledger is Package 4c).
        if any((s, lay, r) not in remaining for s, lay, r in touched if lay != "road"):
            # A track left the view. Deltas only upsert, so the client cannot
            # be told to drop it: it needs a fresh snapshot instead.
            raise QueryRefused(409, "a change removed a track from the view")
        coverage, _, _ = _coverage(conn, query, limits, batch_id)
        # New positions can change which track an event links to, and its
        # last observed position: resend every case in view that names one.
        events = _linked_events(conn, query, limits, touched)
        if len(tracks) > limit or len(coverage) > DELTA_MAX_ITEMS or len(events) > DELTA_MAX_ITEMS:
            raise QueryRefused(413, "batch is larger than one delta allows")
    # A derivation run changes transit counts only; the delta advances the
    # cursor so the client knows to re-read /api/v0/transits.
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "delta",
        "sequence": sequence,
        "cursor": make_cursor(epoch, seq),
        "previous_cursor": make_cursor(epoch, previous_seq),
        "tracks_upserted": tracks,
        "events_upserted": events,
        "coverage_upserted": coverage,
    }


def _check_counts_unchanged(batch_id: str, events: list[dict]) -> None:
    """A batch that touches a case another batch already reported needs a snapshot.

    A served event-coverage count covers the cases in view whose current
    reports came from that batch, and a batch's row is listed when it holds
    one. A new version or a repeated delivery of such a case can change an
    earlier batch's count or whether its row is listed, and a delta can
    neither replace nor remove a coverage row the client already holds, so
    the server resnapshots before the cursor advances. A batch that only adds
    new cases is an ordinary delta.
    """
    others = event_ledger.reporting_batches(events, every_version=True)
    others.discard(batch_id)
    if others:
        raise QueryRefused(409, "a batch touched a case another batch reported; counts may change")


def _check_gaps_unchanged(conn, query: Query, limits: Limits, added: list[tuple]) -> None:
    """A batch whose coverage fills part of an unknown gap needs a snapshot.

    The client holds the gap rows sent earlier, and a delta can only add rows,
    never narrow or remove one. If this batch changes the gaps, the server
    resnapshots before the cursor advances; otherwise the delta stands.
    """
    _, _, full = _coverage(conn, query, limits, None)
    start, end = iso(query.start), iso(query.end)
    remaining = list(full)
    for span in added:
        remaining.remove(span)
    before = event_ledger.coverage_gaps(remaining, query.layers, start, end)
    after = event_ledger.coverage_gaps(full, query.layers, start, end)
    if before != after:
        raise QueryRefused(409, "new event coverage changed the unknown gaps in view")


def _event_delta(conn, query: Query, limits: Limits, batch_id: str) -> list[dict]:
    """Cases an event batch changed that have, or had, any version in view."""
    limit = min(limits.max_events, DELTA_MAX_ITEMS)
    params = _params(query)
    touched = conn.run(
        event_ledger.TOUCHED_CASES,
        batch=batch_id,
        cap=limit + 1,
        **params,
    )
    if len(touched) > limit:
        raise QueryRefused(413, f"batch changes more than {limit} event cases in view")
    if not touched:
        return []
    keys = [f"{s} {lay} {c}" for s, lay, c in touched]
    result = _events(conn, query, limits, keys)
    if len(result) != len(keys):
        # A correction moved a case out of view. Deltas only upsert, so the
        # client needs a fresh snapshot without it.
        raise QueryRefused(409, "a change removed an event from the view")
    return result


def _linked_events(conn, query: Query, limits: Limits, touched) -> list[dict]:
    names = [f"track:{s}:{lay}:{r}" for s, lay, r in touched if lay != "road"]
    if not names:
        return []
    rows = conn.run(
        "SELECT DISTINCT source_id, layer, case_id FROM eye.event_claim "
        "WHERE subject_identifiers && CAST(:names AS text[]) ORDER BY 1, 2, 3 LIMIT :cap",
        names=names,
        cap=DELTA_MAX_ITEMS + 1,
    )
    if not rows:
        return []
    if len(rows) > DELTA_MAX_ITEMS:
        raise QueryRefused(413, "batch changes more event links than one delta allows")
    return _events(conn, query, limits, [f"{s} {lay} {c}" for s, lay, c in rows])


# --- transit counts ----------------------------------------------------------------


def load_lines(directory: Path | None) -> dict[str, object]:
    from eye.worker import transits

    if directory is None:
        return {}
    lines = [transits.load_line(p) for p in sorted(directory.glob("*.json"))]
    return {line.line_id: line for line in lines}


def transit_counts(conn: Connection, line, start: datetime, end: datetime, limits: Limits) -> dict:
    """Counts for one versioned line from the latest derivation run, with cited evidence."""
    from eye.worker.transits import ALGORITHM_VERSION, SOURCE_ID

    if end <= start:
        raise QueryRefused(400, "interval end must be after its start")
    if end - start > timedelta(hours=min(limits.max_interval_hours, limits.max_counts)):
        raise QueryRefused(413, "interval too long for one transit response; narrow the request")
    scope = {"s": SOURCE_ID, "l": line.line_id, "v": line.version, "a": ALGORITHM_VERSION}
    _begin(conn)
    try:
        run = conn.run(
            "SELECT run_id::text, eye.iso_utc(derived_at) FROM eye.derivation_run "
            "WHERE source_id = :s AND line_id = :l AND line_version = :v "
            "AND algorithm_version = :a ORDER BY derived_at DESC, run_id DESC LIMIT 1",
            **scope,
        )
        counts = conn.run(
            "SELECT count_id::text, run_id::text, eye.iso_utc(interval_start), "
            "eye.iso_utc(interval_end), state::text, inbound, outbound, total, "
            "ambiguous_crossings, boundary_crossings, insufficient_gaps, reason, "
            "coverage_ids::text[], crossing_ids::text[], insufficient_gap_ids::text[] "
            "FROM eye.transit_count WHERE source_id = :s AND line_id = :l "
            "AND line_version = :v AND algorithm_version = :a "
            "AND interval_start < :end AND interval_end > :start "
            "ORDER BY interval_start, interval_end LIMIT :cap",
            **scope,
            start=start,
            end=end,
            cap=limits.max_counts + 1,
        )
        if len(counts) > limits.max_counts:
            raise QueryRefused(413, f"more than {limits.max_counts} counts; narrow the request")
        crossing_ids = sorted({c for row in counts for c in row[13]})
        coverage_ids = sorted({c for row in counts for c in row[12]})
        if len(crossing_ids) > limits.max_crossings:
            raise QueryRefused(
                413, f"more than {limits.max_crossings} crossings; narrow the request"
            )
        crossings = conn.run(
            "SELECT crossing_id::text, vessel_id, direction, status, reason, "
            "eye.iso_utc(crossing_time), eye.iso_utc(window_start), eye.iso_utc(window_end), "
            "ST_X(crossing_point), ST_Y(crossing_point), before_observation_id::text, "
            "after_observation_id::text, evidence_batch_ids::text[] FROM eye.line_crossing "
            "WHERE crossing_id = ANY(CAST(:ids AS uuid[])) ORDER BY crossing_time, crossing_id",
            ids=crossing_ids,
        )
        coverage = conn.run(
            "SELECT c.coverage_id::text, c.batch_id::text, eye.iso_utc(c.interval_start), "
            "eye.iso_utc(c.interval_end), c.state::text, c.reason, "
            "eye.iso_utc(b.attempt_finished_at) FROM eye.coverage c "
            "JOIN eye.capture_batch b USING (batch_id) "
            "WHERE c.coverage_id = ANY(CAST(:ids AS uuid[])) ORDER BY c.interval_start, "
            "c.coverage_id",
            ids=coverage_ids,
        )
    finally:
        _end(conn)
    run_id, derived_at = run[0] if run else (None, None)
    message = {
        "schema_version": SCHEMA_VERSION,
        "kind": "transits",
        "generated_at": iso(datetime.now(UTC)),
        "synthetic": bool(line.synthetic) and SOURCE_ID in SYNTHETIC_SOURCES,
        "source": SOURCE_ID,
        "source_label": SOURCE_LABELS.get(SOURCE_ID, SOURCE_ID),
        "line": {
            "id": line.line_id,
            "version": line.version,
            "name": line.name[:500],
            "synthetic": bool(line.synthetic),
            "coords": [list(line.start), list(line.end)],
        },
        "algorithm_version": ALGORITHM_VERSION,
        "run_id": run_id,
        "derived_at": None if derived_at is None else _t(derived_at),
        "interval": {"start": iso(start), "end": iso(end)},
        "counts": [_count(row) for row in counts],
        "crossings": [_crossing(row) for row in crossings],
        "coverage": [
            {
                "id": cid,
                "batch_id": batch,
                "interval": {"start": _t(a), "end": _t(b)},
                "state": state,
                "reason": reason,
                "received_time": _t(received),
            }
            for cid, batch, a, b, state, reason, received in coverage
        ],
    }
    return message


def _count(row) -> dict:
    (cid, run, a, b, state, inbound, outbound, total, amb, bnd, ins, reason, cov, crs, gaps) = row
    return {
        "count_id": cid,
        "run_id": run,
        "interval": {"start": _t(a), "end": _t(b)},
        "state": state,
        "inbound": inbound,
        "outbound": outbound,
        "total": total,
        "ambiguous_crossings": amb,
        "boundary_crossings": bnd,
        "insufficient_gaps": ins,
        "reason": reason,
        "coverage_ids": sorted(cov),
        "crossing_ids": sorted(crs),
        "insufficient_gap_ids": sorted(gaps),
    }


def _crossing(row) -> dict:
    cid, vessel, direction, status, reason, when, w0, w1, lon, lat, before, after, batches = row
    return {
        "id": cid,
        "vessel_id": vessel,
        "direction": direction,
        "status": status,
        "reason": reason,
        "estimated_time": _t(when),
        "time_method": "linear_interpolation",
        "window": {"start": _t(w0), "end": _t(w1)},
        "position": [float(lon), float(lat)],
        "before_observation_id": before,
        "after_observation_id": after,
        "evidence_batch_ids": sorted(batches),
    }


def dumps(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
