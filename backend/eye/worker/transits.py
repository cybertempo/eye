"""Vessel tracks, line crossings and transit counts (algorithm ``transit-counter/1``).

Inputs are only stored evidence: current observation versions of one source
and the coverage rows of its capture batches. The derivation is a pure
function of those inputs (``derive``), so rebuilding the database or replaying
the evidence reproduces the same track, gap, crossing and count ids and values.
``store`` replaces the derived rows for one scope in one transaction and logs
the run append-only; ``verify`` re-derives and compares.

Rules (all distances in metres on a local equirectangular projection centred
on the line; adequate at the few-kilometre scale of one count line):

* Points are excluded from geometry when their version is superseded, their
  publication is contested (current version unknown), or they carry the
  ``low_position_accuracy`` flag.
* A vessel's points split into tracks at a time gap over ``MAX_GAP_S`` or a
  jump implying more than ``MAX_SPEED_MS`` (an identity or quality break).
* Points within ``BAND_M`` of the line have no side. A crossing is a change of
  side between consecutive sided points of one track; its time and point are
  linearly interpolated (estimated, never observed). A crossing within
  ``END_MARGIN_M`` of a line end is ambiguous; beyond the ends it is not a
  crossing of the line.
* A split, track start or track end within ``NEAR_M`` of the line is evidence
  that cannot rule a crossing in or out: sides that differ across it make an
  ambiguous crossing; otherwise it is insufficient evidence.
* A count is exact (qualified) only when usable coverage spans the whole
  interval and nothing is ambiguous or insufficient. Missing, unknown or failed
  coverage, or insufficient evidence, makes it unknown with NULL counts. Partial
  coverage or ambiguous crossings make it partial, and its counts are a lower
  bound.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eye.ingest.capture import iso, stable_id
from eye.storage.db import Connection, transaction

ALGORITHM_VERSION = "transit-counter/1"
SOURCE_ID = "synthetic-ais"
KIND = "transit_count"
MAX_GAP_S = 600.0
MAX_SPEED_MS = 25.0
BAND_M = 50.0
END_MARGIN_M = 250.0
NEAR_M = 2000.0
# A vessel that appears or disappears near the line casts doubt on the hour
# around that moment (after its next report was due), not on all later time.
EDGE_WINDOW_S = 3600.0
METRES_PER_DEGREE = 2 * math.pi * 6_371_008.8 / 360
EXCLUDING_FLAGS = frozenset({"low_position_accuracy"})
STATE_RANK = {"qualified": 3, "partial": 2, "unknown": 1, "failed": 0}
LINES_DIR = Path(__file__).resolve().parents[3] / "reference" / "lines"


class LineDefinitionError(ValueError):
    """A count line file is invalid or conflicts with the stored version."""


# --- line -----------------------------------------------------------------------


@dataclass(frozen=True)
class CountLine:
    line_id: str
    version: int
    name: str
    start: tuple[float, float]  # lon, lat
    end: tuple[float, float]
    inbound_side: str
    synthetic: bool
    sha256: str

    @property
    def wkt(self) -> str:
        return f"LINESTRING({self.start[0]} {self.start[1]}, {self.end[0]} {self.end[1]})"


def load_line(path: Path) -> CountLine:
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
        (lon1, lat1), (lon2, lat2) = doc["coordinates"]
        line = CountLine(
            line_id=doc["line_id"],
            version=int(doc["version"]),
            name=doc["name"],
            start=(float(lon1), float(lat1)),
            end=(float(lon2), float(lat2)),
            inbound_side=doc["inbound_side"],
            synthetic=doc["synthetic"] is True,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LineDefinitionError(f"{path.name}: invalid count line: {exc}") from exc
    if line.inbound_side not in ("left", "right") or line.start == line.end:
        raise LineDefinitionError(f"{path.name}: invalid inbound side or zero-length line")
    if not line.synthetic:
        raise LineDefinitionError(f"{path.name}: only synthetic lines are allowed in Package 2")
    return line


def register_line(conn: Connection, line: CountLine) -> None:
    """Store a line version once; a changed definition under the same version is refused."""
    conn.run(
        "INSERT INTO eye.count_line (line_id, version, name, geometry, inbound_side, synthetic, "
        "definition_sha256) VALUES (:id, :v, :name, ST_GeomFromText(:wkt, 4326), :side, "
        ":synthetic, :sha) ON CONFLICT (line_id, version) DO NOTHING",
        id=line.line_id,
        v=line.version,
        name=line.name,
        wkt=line.wkt,
        side=line.inbound_side,
        synthetic=line.synthetic,
        sha=line.sha256,
    )
    stored = conn.run(
        "SELECT definition_sha256 FROM eye.count_line WHERE line_id = :id AND version = :v",
        id=line.line_id,
        v=line.version,
    )[0][0]
    if stored != line.sha256:
        raise LineDefinitionError(
            f"{line.line_id} v{line.version} is already stored with a different definition; "
            "publish a new version instead"
        )


class Frame:
    """Local metric frame centred on the line."""

    def __init__(self, line: CountLine) -> None:
        self.lon0 = (line.start[0] + line.end[0]) / 2
        self.lat0 = (line.start[1] + line.end[1]) / 2
        self.kx = METRES_PER_DEGREE * math.cos(math.radians(self.lat0))
        self.a = self.xy(*line.start)
        b = self.xy(*line.end)
        dx, dy = b[0] - self.a[0], b[1] - self.a[1]
        self.length = math.hypot(dx, dy)
        self.u = (dx / self.length, dy / self.length)
        self.b = b

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * METRES_PER_DEGREE)

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + x / self.kx, self.lat0 + y / METRES_PER_DEGREE)

    def offset(self, p: tuple[float, float]) -> float:
        """Signed distance from the line's axis; positive is the left side."""
        return self.u[0] * (p[1] - self.a[1]) - self.u[1] * (p[0] - self.a[0])

    def along(self, p: tuple[float, float]) -> float:
        return (p[0] - self.a[0]) * self.u[0] + (p[1] - self.a[1]) * self.u[1]

    def side(self, p: tuple[float, float]) -> int:
        d = self.offset(p)
        return 0 if abs(d) <= BAND_M else (1 if d > 0 else -1)

    def distance_to_line(self, p: tuple[float, float]) -> float:
        t = max(0.0, min(self.length, self.along(p)))
        q = (self.a[0] + t * self.u[0], self.a[1] + t * self.u[1])
        return math.hypot(p[0] - q[0], p[1] - q[1])

    def chord_distance(self, p: tuple[float, float], q: tuple[float, float]) -> float:
        """Distance between the chord p-q and the line segment (0 if they intersect)."""
        dp, dq = self.offset(p), self.offset(q)
        if dp * dq < 0:
            f = dp / (dp - dq)
            x = (p[0] + f * (q[0] - p[0]), p[1] + f * (q[1] - p[1]))
            if 0 <= self.along(x) <= self.length:
                return 0.0
        candidates = [self.distance_to_line(p), self.distance_to_line(q)]
        for end in (self.a, self.b):
            candidates.append(_point_segment(end, p, q))
        return min(candidates)


def _point_segment(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    size = dx * dx + dy * dy
    t = 0.0 if size == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / size))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


# --- inputs ---------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    observation_id: str
    vessel_id: str
    time: datetime
    lon: float
    lat: float
    usable: bool
    batch_ids: tuple[str, ...]


@dataclass(frozen=True)
class CoverageRow:
    coverage_id: str
    start: datetime
    end: datetime
    state: str


@dataclass
class Inputs:
    points: list[Point]
    coverage: list[CoverageRow]
    fingerprint: str


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def read_inputs(conn: Connection, source_id: str, line: CountLine) -> Inputs:
    rows = conn.run(
        """
        SELECT o.observation_id::text, o.source_record_id, o.observed_time,
               ST_X(o.position), ST_Y(o.position), o.quality_flags, v.is_current,
               (SELECT array_agg(r.batch_id::text ORDER BY r.batch_id)
                FROM eye.observation_receipt r WHERE r.observation_id = o.observation_id)
        FROM eye.observation o JOIN eye.observation_version v USING (observation_id)
        WHERE o.source_id = :source AND o.layer = 'vessel'
        ORDER BY o.observation_id
        """,
        source=source_id,
    )
    points = [
        Point(
            observation_id=oid,
            vessel_id=vessel,
            time=_utc(observed),
            lon=lon,
            lat=lat,
            usable=current is True and not (set(flags) & EXCLUDING_FLAGS),
            batch_ids=tuple(batches or ()),
        )
        for oid, vessel, observed, lon, lat, flags, current, batches in rows
    ]
    # Coverage counts only where the capture's requested area contains the line.
    coverage = [
        CoverageRow(cid, _utc(start), _utc(end), state)
        for cid, start, end, state in conn.run(
            """
            SELECT c.coverage_id::text, c.interval_start, c.interval_end, c.state::text
            FROM eye.coverage c JOIN eye.capture_batch b USING (batch_id)
            WHERE c.source_id = :source AND c.layer = 'vessel'
              AND ST_Covers(b.requested_area, ST_GeomFromText(:wkt, 4326))
            ORDER BY c.coverage_id
            """,
            source=source_id,
            wkt=line.wkt,
        )
    ]
    digest = hashlib.sha256()
    for p in points:
        digest.update(f"{p.observation_id}:{int(p.usable)}:{','.join(p.batch_ids)}\n".encode())
    for c in coverage:
        digest.update(f"cov:{c.coverage_id}:{c.state}\n".encode())
    digest.update(f"line:{line.sha256}:{ALGORITHM_VERSION}".encode())
    return Inputs(points, coverage, digest.hexdigest())


# --- derivation -----------------------------------------------------------------


@dataclass
class Derivation:
    tracks: dict[str, tuple] = field(default_factory=dict)
    gaps: dict[str, tuple] = field(default_factory=dict)
    crossings: dict[str, tuple] = field(default_factory=dict)
    counts: dict[str, tuple] = field(default_factory=dict)


def _direction(from_side: int, line: CountLine) -> str:
    inbound_from = 1 if line.inbound_side == "left" else -1
    return "inbound" if from_side == inbound_from else "outbound"


def _crossing(frame, line, p: Point, q: Point, sp: int, sq: int, track_id, status, reason):
    """Crossing between p (side sp) and q (side sq), or None if it misses the line."""
    a, b = frame.xy(p.lon, p.lat), frame.xy(q.lon, q.lat)
    dp, dq = frame.offset(a), frame.offset(b)
    f = dp / (dp - dq)
    x = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
    t = frame.along(x)
    if t < -END_MARGIN_M or t > frame.length + END_MARGIN_M:
        return None
    if status == "definite" and (t < END_MARGIN_M or t > frame.length - END_MARGIN_M):
        status, reason = "ambiguous", "crossing within the end margin of the line"
    when = p.time + (q.time - p.time) * f
    lon, lat = frame.lonlat(*x)
    crossing_id = stable_id(
        "crossing",
        ALGORITHM_VERSION,
        line.line_id,
        str(line.version),
        p.observation_id,
        q.observation_id,
    )
    return crossing_id, (
        p.vessel_id,
        track_id,
        p.observation_id,
        q.observation_id,
        _direction(sp, line),
        status,
        reason,
        iso(when),
        round(lon, 9),
        round(lat, 9),
        tuple(sorted(set(p.batch_ids) | set(q.batch_ids))),
    )


def derive(
    inputs: Inputs, line: CountLine, intervals: list[tuple[datetime, datetime]]
) -> Derivation:
    frame = Frame(line)
    out = Derivation()
    scope = (ALGORITHM_VERSION, line.line_id, str(line.version))
    by_vessel: dict[str, list[Point]] = {}
    for p in inputs.points:
        if p.usable:
            by_vessel.setdefault(p.vessel_id, []).append(p)

    for vessel, pts in sorted(by_vessel.items()):
        pts.sort(key=lambda p: (p.time, p.observation_id))
        tracks: list[list[Point]] = [[pts[0]]]
        for prev, cur in zip(pts, pts[1:], strict=False):
            dt = (cur.time - prev.time).total_seconds()
            a, b = frame.xy(prev.lon, prev.lat), frame.xy(cur.lon, cur.lat)
            reason = None
            if dt > MAX_GAP_S:
                reason = "time_gap"
            elif math.hypot(b[0] - a[0], b[1] - a[1]) / dt > MAX_SPEED_MS:
                reason = "implausible_jump"
            if reason is None:
                tracks[-1].append(cur)
                continue
            tracks.append([cur])
            effect = "none"
            if frame.chord_distance(a, b) <= NEAR_M:
                sa, sb = frame.side(a), frame.side(b)
                if sa and sb and sa != sb:
                    found = _crossing(
                        frame,
                        line,
                        prev,
                        cur,
                        sa,
                        sb,
                        None,
                        "ambiguous",
                        f"{reason.replace('_', ' ')} straddles the line",
                    )
                    effect = "ambiguous_crossing" if found else "insufficient_evidence"
                    if found:
                        out.crossings[found[0]] = found[1]
                else:
                    effect = "insufficient_evidence"
            gap_id = stable_id("gap", *scope, prev.observation_id, cur.observation_id)
            out.gaps[gap_id] = (
                vessel,
                prev.observation_id,
                cur.observation_id,
                iso(prev.time),
                iso(cur.time),
                reason,
                effect,
            )
        # A vessel that appears or disappears near the line may have crossed unseen.
        for edge, point in (("track_start", pts[0]), ("track_end", pts[-1])):
            if frame.distance_to_line(frame.xy(point.lon, point.lat)) <= NEAR_M:
                gap_id = stable_id("gap", *scope, edge, point.observation_id)
                out.gaps[gap_id] = (
                    vessel,
                    point.observation_id if edge == "track_end" else None,
                    point.observation_id if edge == "track_start" else None,
                    iso(point.time) if edge == "track_end" else None,
                    iso(point.time) if edge == "track_start" else None,
                    edge,
                    "insufficient_evidence",
                )

        for track in tracks:
            track_id = stable_id("track", *scope, vessel, track[0].observation_id)
            out.tracks[track_id] = (
                vessel,
                track[0].observation_id,
                track[-1].observation_id,
                iso(track[0].time),
                iso(track[-1].time),
                len(track),
                tuple(p.observation_id for p in track),
            )
            last: tuple[Point, int] | None = None
            for p in track:
                s = frame.side(frame.xy(p.lon, p.lat))
                if s == 0:
                    continue
                if last is not None and s != last[1]:
                    found = _crossing(
                        frame, line, last[0], p, last[1], s, track_id, "definite", None
                    )
                    if found:
                        out.crossings[found[0]] = found[1]
                last = (p, s)

    for start, end in intervals:
        out.counts.update(_count(inputs, line, out, start, end))
    return out


def _coverage_state(coverage: list[CoverageRow], start: datetime, end: datetime):
    """Best coverage state for every instant of [start, end); None where missing."""
    relevant = [c for c in coverage if c.start < end and c.end > start]
    cuts = sorted(
        {start, end}
        | {c.start for c in relevant if start < c.start < end}
        | {c.end for c in relevant if start < c.end < end}
    )
    worst, missing = "qualified", []
    for lo, hi in zip(cuts, cuts[1:], strict=False):
        states = [c.state for c in relevant if c.start <= lo and c.end >= hi]
        best = max(states, key=STATE_RANK.__getitem__) if states else None
        if best is None or STATE_RANK[best] < STATE_RANK["partial"]:
            missing.append((lo, hi, best or "missing"))
        elif STATE_RANK[best] < STATE_RANK[worst]:
            worst = best
    return worst, missing, sorted(c.coverage_id for c in relevant)


def _count(inputs, line, out, start: datetime, end: datetime) -> dict[str, tuple]:
    worst, missing, coverage_ids = _coverage_state(inputs.coverage, start, end)
    lo, hi = iso(start), iso(end)
    definite = {"inbound": 0, "outbound": 0}
    ambiguous = 0
    crossing_ids = []
    for cid, c in sorted(out.crossings.items()):
        if lo <= c[7] < hi:
            crossing_ids.append(cid)
            if c[5] == "definite":
                definite[c[4]] += 1
            else:
                ambiguous += 1
    insufficient = 0
    for g in out.gaps.values():
        if g[6] != "insufficient_evidence":
            continue
        g_start, g_end = g[3], g[4]
        if g[5] == "track_end":  # silent after its next report was due
            g_start, g_end = _shift(g_start, MAX_GAP_S), _shift(g_start, EDGE_WINDOW_S)
        elif g[5] == "track_start":  # silent before it first reported
            g_start, g_end = _shift(g_end, -EDGE_WINDOW_S), _shift(g_end, -MAX_GAP_S)
        overlaps = g_start < hi and g_end > lo
        insufficient += overlaps

    if missing:
        what = "; ".join(f"{iso(a)} to {iso(b)} {state}" for a, b, state in missing)
        state, reason, counts = "unknown", f"no usable coverage: {what}", (None, None, None)
    elif insufficient:
        state = "unknown"
        reason = f"{insufficient} track gap(s) near the line could hide crossings"
        counts = (None, None, None)
    else:
        total = definite["inbound"] + definite["outbound"]
        counts = (definite["inbound"], definite["outbound"], total)
        notes = []
        if ambiguous:
            notes.append(f"{ambiguous} ambiguous crossing(s) not counted")
        if worst == "partial":
            notes.append("coverage is partial")
        state = "partial" if notes else "qualified"
        reason = f"lower bound: {'; '.join(notes)}" if notes else None
    count_id = stable_id(
        "count",
        ALGORITHM_VERSION,
        line.line_id,
        str(line.version),
        lo,
        hi,
    )
    return {
        count_id: (
            lo,
            hi,
            state,
            *counts,
            ambiguous,
            insufficient,
            reason,
            tuple(coverage_ids),
            tuple(crossing_ids),
        )
    }


def _shift(stamp: str, seconds: float) -> str:
    moment = datetime.fromisoformat(stamp) + timedelta(seconds=seconds)
    return iso(moment)


# --- storage --------------------------------------------------------------------


def hourly_intervals(inputs: Inputs) -> list[tuple[datetime, datetime]]:
    """Whole UTC hours spanning every coverage row of the source."""
    if not inputs.coverage:
        return []
    first = min(c.start for c in inputs.coverage).replace(minute=0, second=0, microsecond=0)
    last = max(c.end for c in inputs.coverage)
    out, t = [], first
    while t < last:
        out.append((t, t + timedelta(hours=1)))
        t += timedelta(hours=1)
    return out


def _stored_intervals(conn, source_id, line) -> list[tuple[datetime, datetime]]:
    rows = conn.run(
        "SELECT interval_start, interval_end FROM eye.transit_count WHERE source_id = :s "
        "AND line_id = :l AND line_version = :v AND algorithm_version = :a",
        s=source_id,
        l=line.line_id,
        v=line.version,
        a=ALGORITHM_VERSION,
    )
    return [(_utc(a), _utc(b)) for a, b in rows]


def store(
    conn: Connection,
    source_id: str,
    line: CountLine,
    intervals: list[tuple[datetime, datetime]],
) -> tuple[str, Derivation]:
    """Derive and replace the stored results for this scope; returns (run_id, derivation)."""
    with transaction(conn):
        conn.run(
            "SELECT pg_advisory_xact_lock(hashtext(:k))", k=f"{KIND}:{source_id}:{line.line_id}"
        )
        register_line(conn, line)
        wanted = sorted(set(intervals) | set(_stored_intervals(conn, source_id, line)))
        inputs = read_inputs(conn, source_id, line)
        derived = derive(inputs, line, wanted)
        run_id = stable_id(
            "run",
            KIND,
            source_id,
            line.line_id,
            str(line.version),
            ALGORITHM_VERSION,
            inputs.fingerprint,
            json.dumps([(iso(a), iso(b)) for a, b in wanted]),
        )
        summary = {
            "intervals": [
                {"start": c[0], "end": c[1], "state": c[2], "total": c[5]}
                for c in sorted(derived.counts.values())
            ],
            "crossings": len(derived.crossings),
            "tracks": len(derived.tracks),
        }
        conn.run(
            "INSERT INTO eye.derivation_run (run_id, kind, source_id, line_id, line_version, "
            "algorithm_version, input_fingerprint, observation_count, coverage_ids, summary) "
            "VALUES (:id, :kind, :s, :l, :v, :a, :fp, :n, CAST(:cov AS uuid[]), "
            "CAST(:summary AS jsonb)) ON CONFLICT (run_id) DO NOTHING",
            id=run_id,
            kind=KIND,
            s=source_id,
            l=line.line_id,
            v=line.version,
            a=ALGORITHM_VERSION,
            fp=inputs.fingerprint,
            n=len(inputs.points),
            cov=sorted(c.coverage_id for c in inputs.coverage),
            summary=json.dumps(summary, sort_keys=True),
        )
        scope = {"s": source_id, "l": line.line_id, "v": line.version, "a": ALGORITHM_VERSION}
        conn.run(
            "DELETE FROM eye.transit_count WHERE "
            "source_id = :s AND line_id = :l AND line_version = :v AND algorithm_version = :a",
            **scope,
        )
        conn.run(
            "DELETE FROM eye.line_crossing WHERE "
            "source_id = :s AND line_id = :l AND line_version = :v AND algorithm_version = :a",
            **scope,
        )
        conn.run(
            "DELETE FROM eye.track_gap WHERE "
            "source_id = :s AND line_id = :l AND line_version = :v AND algorithm_version = :a",
            **scope,
        )
        conn.run(
            "DELETE FROM eye.vessel_track WHERE "
            "source_id = :s AND line_id = :l AND line_version = :v AND algorithm_version = :a",
            **scope,
        )
        _insert(conn, run_id, source_id, line, derived)
    return run_id, derived


def _insert(conn, run_id, source_id, line, d: Derivation) -> None:
    common = {
        "run": run_id,
        "s": source_id,
        "l": line.line_id,
        "v": line.version,
        "a": ALGORITHM_VERSION,
    }
    for tid, (vessel, first, last, t0, t1, n, obs) in d.tracks.items():
        conn.run(
            "INSERT INTO eye.vessel_track (track_id, run_id, source_id, vessel_id, line_id, "
            "line_version, algorithm_version, first_observation_id, last_observation_id, "
            "start_time, end_time, point_count, observation_ids) VALUES (:id, :run, :s, :vessel, "
            ":l, :v, :a, :first, :last, CAST(:t0 AS timestamptz), CAST(:t1 AS timestamptz), :n, "
            "CAST(:obs AS uuid[]))",
            id=tid,
            vessel=vessel,
            first=first,
            last=last,
            t0=t0,
            t1=t1,
            n=n,
            obs=list(obs),
            **common,
        )
    for gid, (vessel, before, after, g0, g1, reason, effect) in d.gaps.items():
        conn.run(
            "INSERT INTO eye.track_gap (gap_id, run_id, source_id, vessel_id, line_id, "
            "line_version, algorithm_version, before_observation_id, after_observation_id, "
            "gap_start, gap_end, reason, effect) VALUES (:id, :run, :s, :vessel, :l, :v, :a, "
            "CAST(:before AS uuid), CAST(:after AS uuid), CAST(:g0 AS timestamptz), "
            "CAST(:g1 AS timestamptz), :reason, :effect)",
            id=gid,
            vessel=vessel,
            before=before,
            after=after,
            g0=g0,
            g1=g1,
            reason=reason,
            effect=effect,
            **common,
        )
    for cid, c in d.crossings.items():
        vessel, track, before, after, direction, status, reason, when, lon, lat, batches = c
        conn.run(
            "INSERT INTO eye.line_crossing (crossing_id, run_id, source_id, vessel_id, line_id, "
            "line_version, algorithm_version, track_id, before_observation_id, "
            "after_observation_id, direction, status, reason, crossing_time, time_method, "
            "crossing_point, evidence_batch_ids) VALUES (:id, :run, :s, :vessel, :l, :v, :a, "
            "CAST(:track AS uuid), :before, :after, :direction, :status, :reason, "
            "CAST(:when AS timestamptz), 'linear_interpolation', "
            "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), CAST(:batches AS uuid[]))",
            id=cid,
            vessel=vessel,
            track=track,
            before=before,
            after=after,
            direction=direction,
            status=status,
            reason=reason,
            when=when,
            lon=lon,
            lat=lat,
            batches=list(batches),
            **common,
        )
    for cid, c in d.counts.items():
        lo, hi, state, inbound, outbound, total, ambiguous, insufficient, reason, cov, xs = c
        conn.run(
            "INSERT INTO eye.transit_count (count_id, run_id, source_id, line_id, line_version, "
            "algorithm_version, interval_start, interval_end, state, inbound, outbound, total, "
            "ambiguous_crossings, insufficient_gaps, reason, coverage_ids, crossing_ids) VALUES "
            "(:id, :run, :s, :l, :v, :a, CAST(:lo AS timestamptz), CAST(:hi AS timestamptz), "
            "CAST(:state AS eye.coverage_state), :inbound, :outbound, :total, :ambiguous, "
            ":insufficient, :reason, CAST(:cov AS uuid[]), CAST(:xs AS uuid[]))",
            id=cid,
            lo=lo,
            hi=hi,
            state=state,
            inbound=inbound,
            outbound=outbound,
            total=total,
            ambiguous=ambiguous,
            insufficient=insufficient,
            reason=reason,
            cov=list(cov),
            xs=list(xs),
            **common,
        )


# --- verification ---------------------------------------------------------------


def _norm(value):
    if isinstance(value, list):
        return tuple(value)
    return value


def read_stored(conn: Connection, source_id: str, line: CountLine) -> Derivation:
    scope = {"s": source_id, "l": line.line_id, "v": line.version, "a": ALGORITHM_VERSION}
    d = Derivation()
    for row in conn.run(
        "SELECT track_id::text, vessel_id, first_observation_id::text, "
        "last_observation_id::text, eye.iso_utc(start_time), eye.iso_utc(end_time), "
        "point_count, observation_ids::text[] FROM eye.vessel_track "
        "WHERE source_id = :s AND line_id = :l AND line_version = :v "
        "AND algorithm_version = :a",
        **scope,
    ):
        d.tracks[row[0]] = tuple(_norm(v) for v in row[1:])
    for row in conn.run(
        "SELECT gap_id::text, vessel_id, before_observation_id::text, "
        "after_observation_id::text, eye.iso_utc(gap_start), eye.iso_utc(gap_end), "
        "reason, effect FROM eye.track_gap "
        "WHERE source_id = :s AND line_id = :l AND line_version = :v "
        "AND algorithm_version = :a",
        **scope,
    ):
        d.gaps[row[0]] = tuple(row[1:])
    for row in conn.run(
        "SELECT crossing_id::text, vessel_id, track_id::text, before_observation_id::text, "
        "after_observation_id::text, direction, status, reason, eye.iso_utc(crossing_time), "
        "ST_X(crossing_point), ST_Y(crossing_point), evidence_batch_ids::text[] "
        "FROM eye.line_crossing "
        "WHERE source_id = :s AND line_id = :l AND line_version = :v "
        "AND algorithm_version = :a",
        **scope,
    ):
        d.crossings[row[0]] = tuple(_norm(v) for v in row[1:])
    for row in conn.run(
        "SELECT count_id::text, eye.iso_utc(interval_start), eye.iso_utc(interval_end), "
        "state::text, inbound, outbound, total, ambiguous_crossings, insufficient_gaps, reason, "
        "coverage_ids::text[], crossing_ids::text[] FROM eye.transit_count "
        "WHERE source_id = :s AND line_id = :l AND line_version = :v "
        "AND algorithm_version = :a",
        **scope,
    ):
        d.counts[row[0]] = tuple(_norm(v) for v in row[1:])
    return d


def verify(conn: Connection, source_id: str, line: CountLine) -> list[str]:
    """Re-derive from current evidence and compare with every stored derived value."""
    problems: list[str] = []
    intervals = _stored_intervals(conn, source_id, line)
    inputs = read_inputs(conn, source_id, line)
    expected = derive(inputs, line, sorted(set(intervals)))
    stored = read_stored(conn, source_id, line)
    for label in ("tracks", "gaps", "crossings", "counts"):
        want, got = getattr(expected, label), getattr(stored, label)
        for key in sorted(set(want) | set(got)):
            if key not in got:
                problems.append(f"{label} {key}: derivable from evidence but not stored")
            elif key not in want:
                problems.append(f"{label} {key}: stored but not derivable from current evidence")
            elif want[key] != got[key]:
                problems.append(f"{label} {key}: stored {got[key]!r}, derived {want[key]!r}")
    return problems
