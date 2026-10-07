"""Replayable daily rollups and their derivation manifests (Package 5).

A *partition* is one source, one layer and one UTC day. For each partition
the worker computes the derivations it requires and records each one as an
append-only manifest (``eye.derivation_manifest``) with its rollup rows
(``eye.rollup_value``):

* ``coverage-hourly``: for every hour and for the day, how many seconds the
  source's captures were qualified, partial, unknown or failed, and how many
  were not captured at all. This is an exact account of EYE's own capture
  log, so each row is qualified; its value is the usable (qualified or
  partial) seconds.
* ``observations-hourly`` (position sources): the number of distinct observed
  states (record and observed time) per hour and for the day. An hour with
  any second lacking usable coverage is unknown with no value, never 0; an
  hour with partial coverage is a lower bound; only a fully qualified hour is
  exact, so a covered hour with nothing observed is a genuine 0.
* ``cells-daily`` (position sources): current observed states per 0.1 degree
  grid cell. A cell is named by its index and bounds; no row carries a mean,
  centre or any other coordinate that could be read as a vessel's or
  aircraft's position. Only cells with observations have rows: a cell
  without a row is not a measured zero.
* ``transit-daily`` (``synthetic-ais`` vessels, per count line): hourly counts
  copied from the latest recorded transit run and their day total under the
  transit counter's rules (any unknown hour makes the day unknown).

Every derivation is a pure function of stored evidence (observations,
version history, coverage, recorded transit runs), never of raw bytes, so
rollups survive pruning and a restore reproduces them. A manifest's id is
derived from its partition, derivation, version, scope and input checksum:
re-running on unchanged evidence is a no-op, and a late arrival or
correction yields a new manifest while the old one stays as history.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from pg8000.exceptions import DatabaseError, InterfaceError

from eye.ingest import capture
from eye.ingest.capture import iso, stable_id
from eye.storage.db import Connection, transaction
from eye.worker import transits

COVERAGE = "coverage-hourly"
OBSERVATIONS = "observations-hourly"
CELLS = "cells-daily"
TRANSIT = "transit-daily"
LEDGER = "ledger-replay"
VERSIONS = {
    COVERAGE: "coverage-hourly/2",
    OBSERVATIONS: "observations-hourly/2",
    CELLS: "cells-daily/2",
    TRANSIT: "transit-daily/1",
    LEDGER: "ledger-replay/1",
}
CELL_DEG = Decimal("0.1")
MAX_CELLS = 5_000
MAX_INPUT_ROWS = 200_000
DEFAULT_LATENESS_HOURS = 48
STATE_RANK = transits.STATE_RANK
COVERAGE_STATES = ("qualified", "partial", "unknown", "failed")
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


class RollupRefused(ValueError):
    """A derivation cannot be computed within its bounds or from its inputs."""


class RollupStale(RollupRefused):
    """A derivation's upstream run does not include all current evidence."""


@dataclass(frozen=True, order=True)
class Partition:
    source_id: str
    layer: str
    day: date

    @property
    def start(self) -> datetime:
        return datetime(self.day.year, self.day.month, self.day.day, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        return self.start + DAY

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.layer}:{self.day.isoformat()}"

    @classmethod
    def parse(cls, key: str) -> Partition:
        parts = key.split(":")
        if len(parts) != 3 or not all(parts):
            raise ValueError("a partition is SOURCE:LAYER:YYYY-MM-DD")
        if not capture.IDENTIFIER.match(parts[0]) or parts[1] not in capture.LAYERS:
            raise ValueError(f"unknown source or layer in partition {key!r}")
        return cls(parts[0], parts[1], date.fromisoformat(parts[2]))


@dataclass
class Rollup:
    """One computed derivation of one partition (not yet stored)."""

    partition: Partition
    derivation: str
    scope: str
    input_batch_ids: tuple[str, ...]
    input_row_count: int
    input_sha256: str
    watermark: datetime | None
    rows: dict[str, tuple] = field(default_factory=dict)
    coverage_summary: dict = field(default_factory=dict)
    transit_run_id: str | None = None

    @property
    def version(self) -> str:
        return VERSIONS[self.derivation]

    @property
    def output_sha256(self) -> str:
        return output_checksum(self.rows)

    @property
    def manifest_id(self) -> str:
        p = self.partition
        return stable_id(
            "manifest",
            p.source_id,
            p.layer,
            p.day.isoformat(),
            self.derivation,
            self.version,
            self.scope,
            self.input_sha256,
        )


@dataclass(frozen=True)
class ManifestCheck:
    partition: Partition
    derivation: str
    scope: str
    manifest_id: str | None
    state: str  # valid | stale | failed | unverified
    reason: str | None

    @property
    def label(self) -> str:
        scope = f" {self.scope}" if self.scope else ""
        return f"{self.derivation}{scope} {self.partition.key}"


# --- helpers ----------------------------------------------------------------------


def num(value) -> int | float | None:
    """Canonical number for checksums: ints stay ints, others round to 6 places."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        rounded = round(value, 6)
        return int(rounded) if rounded == int(rounded) else rounded
    return int(value)


def _canonical_detail(detail: dict) -> dict:
    out = {}
    for key, value in sorted(detail.items()):
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            out[key] = num(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [num(v) if isinstance(v, (int, float, Decimal)) else v for v in value]
        else:
            out[key] = value
    return out


def canonical_rows(rows: dict[str, tuple]) -> list:
    return [
        [key, start, end, metric, state, num(value), reason, _canonical_detail(detail)]
        for key, (start, end, metric, state, value, reason, detail) in sorted(rows.items())
    ]


def output_checksum(rows: dict[str, tuple]) -> str:
    text = json.dumps(canonical_rows(rows), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def line_scope(line: transits.CountLine) -> str:
    return f"line:{line.line_id}/v{line.version}"


def is_position_source(source_id: str) -> bool:
    return capture.SOURCE_FORMATS.get(source_id) == capture.CAPTURE_FORMAT


def required(partition: Partition, lines: list[transits.CountLine]) -> list[tuple[str, str]]:
    """The derivations a partition day needs before its raw evidence may be pruned.

    ``ledger-replay`` concerns the batches that start on the day; the others
    concern every batch whose request overlaps it.
    """
    if partition.source_id not in capture.SOURCE_FORMATS:
        raise RollupRefused(f"no derivations are defined for source {partition.source_id}")
    out = [(LEDGER, ""), (COVERAGE, "")]
    if is_position_source(partition.source_id):
        out += [(OBSERVATIONS, ""), (CELLS, "")]
        if partition.source_id == transits.SOURCE_ID and partition.layer == "vessel":
            out += [(TRANSIT, line_scope(line)) for line in lines]
    return out


@contextlib.contextmanager
def snapshot(conn: Connection) -> Iterator[Connection]:
    """A read-only, repeatable-read transaction: every query sees one state."""
    conn.run("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        yield conn
    finally:
        conn.run("ROLLBACK")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _seconds(delta: timedelta) -> Decimal:
    return Decimal(
        delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    ) / Decimal(1_000_000)


@dataclass(frozen=True)
class CoverageRow:
    coverage_id: str
    batch_id: str
    start: datetime
    end: datetime
    state: str
    area: tuple[float, float, float, float]


Area = tuple[float, float, float, float]  # west, south, east, north (a requested rectangle)
AREA_RANK = {"uncaptured": -1, **STATE_RANK}


def area_state(footprint: tuple[Area, ...], active: tuple[tuple[Area, str], ...]) -> str:
    """The weakest state anywhere in the footprint, given the captures active at one instant.

    Every point of the footprint takes the best state of the captures whose
    requested rectangle contains it, or ``uncaptured`` when none does; the
    footprint's state is the worst of those. A qualified capture of one area
    therefore never vouches for a simultaneous failed or missing capture of
    another. Requested areas are rectangles (``ST_MakeEnvelope``), so the
    elementary cells between all rectangle edges decide this exactly.
    """
    if not footprint:
        return "uncaptured"
    xs = sorted({a[i] for a in (*footprint, *(r for r, _ in active)) for i in (0, 2)})
    ys = sorted({a[i] for a in (*footprint, *(r for r, _ in active)) for i in (1, 3)})
    worst = "qualified"
    for x0, x1 in zip(xs, xs[1:], strict=False):
        mx = (x0 + x1) / 2
        for y0, y1 in zip(ys, ys[1:], strict=False):
            my = (y0 + y1) / 2

            def inside(a: Area, mx=mx, my=my) -> bool:
                return a[0] <= mx <= a[2] and a[1] <= my <= a[3]

            if not any(inside(a) for a in footprint):
                continue
            states = [s for a, s in active if inside(a)]
            here = max(states, key=AREA_RANK.__getitem__) if states else "uncaptured"
            if AREA_RANK[here] < AREA_RANK[worst]:
                worst = here
                if worst == "uncaptured":
                    return worst
    return worst


def accounting(
    rows: list[CoverageRow],
    start: datetime,
    end: datetime,
    footprint: tuple[Area, ...] | None = None,
) -> dict:
    """Seconds of [start, end) by the weakest coverage state over the footprint.

    The footprint is every area requested in ``rows`` (the partition's whole
    requested area for the day) unless given. At each instant the state is
    the weakest over the footprint (see ``area_state``), so a capture that
    covers only part of it cannot report the whole as qualified.
    """
    if footprint is None:
        footprint = tuple(sorted({c.area for c in rows}))
    relevant = [c for c in rows if c.start < end and c.end > start]
    cuts = sorted(
        {start, end}
        | {c.start for c in relevant if start < c.start < end}
        | {c.end for c in relevant if start < c.end < end}
    )
    totals = {state: Decimal(0) for state in (*COVERAGE_STATES, "uncaptured")}
    memo: dict[tuple, str] = {}
    for lo, hi in zip(cuts, cuts[1:], strict=False):
        active = tuple(
            sorted({(c.area, c.state) for c in relevant if c.start <= lo and c.end >= hi})
        )
        if active not in memo:
            memo[active] = area_state(footprint, active)
        totals[memo[active]] += _seconds(hi - lo)
    return {
        **{f"{state}_s": num(value) for state, value in totals.items()},
        "coverage_ids": sorted(c.coverage_id for c in relevant),
    }


def _missing_s(acc: dict) -> Decimal:
    return (
        Decimal(str(acc["unknown_s"]))
        + Decimal(str(acc["failed_s"]))
        + Decimal(str(acc["uncaptured_s"]))
    )


def _hours(partition: Partition) -> list[tuple[str, datetime, datetime]]:
    return [
        (f"hour:{h:02d}", partition.start + h * HOUR, partition.start + (h + 1) * HOUR)
        for h in range(24)
    ]


# --- inputs -----------------------------------------------------------------------


def _batches(conn: Connection, partition: Partition) -> list[tuple[str, datetime, str]]:
    """Settled batches of the source and layer whose request overlaps the day."""
    return [
        (str(batch_id), _utc(archived), status)
        for batch_id, archived, status in conn.run(
            "SELECT batch_id::text, archived_at, status::text FROM eye.capture_batch "
            "WHERE source_id = :s "
            "AND layer = :l AND status <> 'pending' AND requested_start < :e "
            "AND requested_end > :b ORDER BY batch_id",
            s=partition.source_id,
            l=partition.layer,
            b=partition.start,
            e=partition.end,
        )
    ]


def _coverage(conn: Connection, partition: Partition, batch_ids: list[str]) -> list[CoverageRow]:
    return [
        CoverageRow(cid, bid, _utc(start), _utc(end), state, (w, s, e, n))
        for cid, bid, start, end, state, w, s, e, n in conn.run(
            "SELECT c.coverage_id::text, c.batch_id::text, c.interval_start, c.interval_end, "
            "c.state::text, ST_XMin(b.requested_area), ST_YMin(b.requested_area), "
            "ST_XMax(b.requested_area), ST_YMax(b.requested_area) "
            "FROM eye.coverage c JOIN eye.capture_batch b USING (batch_id) "
            "WHERE c.layer = :l AND c.batch_id = ANY(CAST(:batches AS uuid[])) "
            "ORDER BY c.coverage_id",
            l=partition.layer,
            batches=batch_ids,
        )
    ]


@dataclass(frozen=True)
class State:
    """One observed state: every version of one record at one observed time."""

    record: str
    time: datetime
    observation_ids: tuple[str, ...]
    current: tuple[float, float] | None  # position of the unique current version
    superseded: int
    contested: bool


def _states(conn: Connection, partition: Partition) -> list[State]:
    rows = conn.run(
        "SELECT o.observation_id::text, o.source_record_id, o.observed_time, ST_X(o.position), "
        "ST_Y(o.position), v.is_current FROM eye.observation o "
        "JOIN eye.observation_version v USING (observation_id) "
        "WHERE o.source_id = :s AND o.layer = :l AND o.observed_time >= :b "
        "AND o.observed_time < :e ORDER BY o.observation_id LIMIT :cap",
        s=partition.source_id,
        l=partition.layer,
        b=partition.start,
        e=partition.end,
        cap=MAX_INPUT_ROWS + 1,
    )
    if len(rows) > MAX_INPUT_ROWS:
        raise RollupRefused(
            f"{partition.key}: more than {MAX_INPUT_ROWS} observations in one day; refused"
        )
    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault((row[1], _utc(row[2])), []).append(row)
    out = []
    for (record, when), group in sorted(groups.items()):
        current = [r for r in group if r[5] is True]
        out.append(
            State(
                record=record,
                time=when,
                observation_ids=tuple(sorted(r[0] for r in group)),
                current=(current[0][3], current[0][4]) if current else None,
                superseded=sum(1 for r in group if r[5] is False),
                contested=any(r[5] is None for r in group),
            )
        )
    return out


def _digest(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode() + b"\n")
    return digest.hexdigest()


# --- derivations ------------------------------------------------------------------


def compute(
    conn: Connection,
    partition: Partition,
    derivation: str,
    scope: str,
    lines: list[transits.CountLine],
) -> Rollup:
    """Compute one derivation from stored evidence. Reads only; never writes."""
    if derivation == LEDGER:
        return _ledger(conn, partition)
    batches = _batches(conn, partition)
    batch_ids = [b for b, _, _ in batches]
    committed = [b for b, _, status in batches if status == "committed"]
    watermark = max((a for _, a, _ in batches), default=None)
    coverage = _coverage(conn, partition, batch_ids)
    head = [f"derivation:{derivation}:{VERSIONS[derivation]}:{scope}"]
    head += [f"batch:{b}" for b in batch_ids]
    head += [f"cov:{c.coverage_id}:{c.state}" for c in coverage]
    day_acc = accounting(coverage, partition.start, partition.end)
    summary = {k: v for k, v in day_acc.items() if k != "coverage_ids"}
    if derivation == COVERAGE:
        rows = _coverage_rows(partition, coverage)
        return Rollup(
            partition,
            derivation,
            scope,
            tuple(batch_ids),
            len(coverage),
            _digest(head),
            watermark,
            rows,
            summary,
        )
    if derivation in (OBSERVATIONS, CELLS):
        if not is_position_source(partition.source_id):
            raise RollupRefused(f"{partition.source_id} delivers no positions")
        states = _states(conn, partition)
        parts = head + [
            f"state:{s.record}:{iso(s.time)}:{','.join(s.observation_ids)}:"
            f"{s.current}:{s.superseded}:{int(s.contested)}"
            for s in states
        ]
        maker = _observation_rows if derivation == OBSERVATIONS else _cell_rows
        rows = maker(partition, coverage, states)
        n_obs = sum(len(s.observation_ids) for s in states)
        return Rollup(
            partition,
            derivation,
            scope,
            tuple(batch_ids),
            len(coverage) + n_obs,
            _digest(parts),
            watermark,
            rows,
            summary,
        )
    if derivation == TRANSIT:
        return _transit(
            conn, partition, scope, lines, batch_ids, committed, head, watermark, summary
        )
    raise RollupRefused(f"unknown derivation {derivation}")


def _ledger(conn: Connection, partition: Partition) -> Rollup:
    """Re-derive every ledger row the partition's own batches produced from their bytes.

    The partition's own batches are those whose requested start falls on its
    day (the batches retention would prune). Their outcomes, observations,
    event claims, media items, receipts, coverage and capture facts must be
    reproduced exactly from the stored bytes; any difference refuses the
    derivation, so a corrupted derived row blocks pruning. Once the bytes are
    pruned this derivation cannot run (EvidencePruned); only the restore drill,
    from the backup, can re-derive it.
    """
    own = [
        (str(b), _utc(archived), sha)
        for b, archived, sha in conn.run(
            "SELECT batch_id::text, archived_at, evidence_sha256 FROM eye.capture_batch "
            "WHERE source_id = :s AND layer = :l AND status <> 'pending' "
            "AND (requested_start AT TIME ZONE 'UTC')::date = :d ORDER BY batch_id",
            s=partition.source_id,
            l=partition.layer,
            d=partition.day,
        )
    ]
    batch_ids = [b for b, _, _ in own]
    stored, problems = capture.replay_batches(conn, batch_ids)
    if problems:
        detail = f"{len(problems)} ledger row(s) differ from their evidence bytes"
        raise RollupRefused(f"{detail}, e.g. {problems[0]}"[:480])
    head = [f"derivation:{LEDGER}:{VERSIONS[LEDGER]}:"]
    head += [f"evidence:{b}:{sha}" for b, _, sha in own]
    rows: dict[str, tuple] = {}
    n_rows = 0
    for kind in capture.LEDGER_KINDS:
        values = stored[kind]
        n_rows += len(values)
        text = json.dumps(
            sorted([list(k) if isinstance(k, tuple) else k, v] for k, v in values.items()),
            default=str,
            separators=(",", ":"),
        )
        rows[f"rows:{kind.replace(' ', '-')}"] = (
            iso(partition.start),
            iso(partition.end),
            "ledger_rows",
            "qualified",
            len(values),
            None,
            {"sha256": hashlib.sha256(text.encode()).hexdigest()},
        )
    return Rollup(
        partition,
        LEDGER,
        "",
        tuple(batch_ids),
        n_rows,
        _digest(head),
        max((a for _, a, _ in own), default=None),
        rows,
        {"batches": len(batch_ids)},
    )


def _coverage_rows(partition: Partition, coverage: list[CoverageRow]) -> dict[str, tuple]:
    rows = {}
    for key, start, end in [*_hours(partition), ("day", partition.start, partition.end)]:
        acc = accounting(coverage, start, end)
        usable = Decimal(str(acc["qualified_s"])) + Decimal(str(acc["partial_s"]))
        rows[key] = (
            iso(start),
            iso(end),
            "usable_coverage_seconds",
            "qualified",
            num(usable),
            None,
            acc,
        )
    return rows


def _observation_rows(partition, coverage, states: list[State]) -> dict[str, tuple]:
    rows: dict[str, tuple] = {}
    hours = []
    for key, start, end in _hours(partition):
        acc = accounting(coverage, start, end)
        missing = _missing_s(acc)
        in_hour = [s for s in states if start <= s.time < end]
        if missing > 0:
            reason = (
                f"no usable coverage for {num(missing)} s of this interval "
                f"(unknown {acc['unknown_s']} s, failed {acc['failed_s']} s, "
                f"not captured {acc['uncaptured_s']} s)"
            )
            rows[key] = (
                iso(start),
                iso(end),
                "observed_states",
                "unknown",
                None,
                reason,
                {"coverage_ids": acc["coverage_ids"]},
            )
            hours.append(("unknown", 0, set()))
            continue
        detail = {
            "records": len({s.record for s in in_hour}),
            "superseded_versions": sum(s.superseded for s in in_hour),
            "contested_states": sum(1 for s in in_hour if s.contested),
            "coverage_ids": acc["coverage_ids"],
        }
        if Decimal(str(acc["partial_s"])) > 0:
            state = "partial"
            reason = f"lower bound: coverage is partial for {acc['partial_s']} s"
        else:
            state, reason = "qualified", None
        rows[key] = (iso(start), iso(end), "observed_states", state, len(in_hour), reason, detail)
        hours.append((state, len(in_hour), {s.record for s in in_hour}))
    unknown = sum(1 for h in hours if h[0] == "unknown")
    if unknown:
        rows["day"] = (
            iso(partition.start),
            iso(partition.end),
            "observed_states",
            "unknown",
            None,
            f"{unknown} hour(s) without usable coverage",
            {},
        )
    else:
        partial = sum(1 for h in hours if h[0] == "partial")
        total = sum(h[1] for h in hours)
        records = set().union(*(h[2] for h in hours))
        state = "partial" if partial else "qualified"
        reason = f"lower bound: {partial} hour(s) with partial coverage" if partial else None
        rows["day"] = (
            iso(partition.start),
            iso(partition.end),
            "observed_states",
            state,
            total,
            reason,
            {"records": len(records)},
        )
    return rows


def _cell(value: float) -> int:
    return int((Decimal(str(value)) / CELL_DEG).to_integral_value(rounding=ROUND_FLOOR))


def _cell_rows(partition, coverage, states: list[State]) -> dict[str, tuple]:
    cells: dict[tuple[int, int], list[State]] = {}
    unplaced = 0
    for s in states:
        if s.current is None:
            unplaced += 1
            continue
        lon, lat = s.current
        cells.setdefault((_cell(lat), _cell(lon)), []).append(s)
    if len(cells) > MAX_CELLS:
        raise RollupRefused(
            f"{partition.key}: {len(cells)} occupied cells exceed the limit of {MAX_CELLS}; "
            "refused rather than cut short"
        )
    rows: dict[str, tuple] = {}
    for (iy, ix), members in sorted(cells.items()):
        west, south = ix * CELL_DEG, iy * CELL_DEG
        bounds = [num(west), num(south), num(west + CELL_DEG), num(south + CELL_DEG)]
        cell = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
        covering = [
            c
            for c in coverage
            if c.area[0] < cell[2]
            and c.area[2] > cell[0]
            and c.area[1] < cell[3]
            and c.area[3] > cell[1]
        ]
        acc = accounting(covering, partition.start, partition.end, footprint=(cell,))
        if Decimal(str(acc["qualified_s"])) == _seconds(DAY):
            state, reason = "qualified", None
        else:
            state = "partial"
            reason = (
                f"lower bound: the whole cell was qualified for {acc['qualified_s']} of 86400 s"
            )
        detail = {
            "cell_deg": num(CELL_DEG),
            "bounds": bounds,
            "records": len({s.record for s in members}),
            "coverage_ids": acc["coverage_ids"],
        }
        rows[f"cell:lat{iy}:lon{ix}"] = (
            iso(partition.start),
            iso(partition.end),
            "observed_states",
            state,
            len(members),
            reason,
            detail,
        )
    if unplaced:
        rows["unplaced"] = (
            iso(partition.start),
            iso(partition.end),
            "contested_states",
            "qualified",
            unplaced,
            "latest versions conflict; no position is chosen, so no cell is credited",
            {},
        )
    return rows


def _transit(
    conn, partition, scope, lines, batch_ids, committed, head, watermark, summary
) -> Rollup:
    matches = [line for line in lines if line_scope(line) == scope]
    if len(matches) != 1:
        raise RollupRefused(f"no configured count line for {scope}")
    line = matches[0]
    run = conn.run(
        "SELECT run_id::text, input_batch_ids::text[] FROM eye.derivation_run "
        "WHERE source_id = :s AND line_id = :l AND line_version = :v AND algorithm_version = :a "
        "ORDER BY derived_at DESC, run_id DESC LIMIT 1",
        s=partition.source_id,
        l=line.line_id,
        v=line.version,
        a=transits.ALGORITHM_VERSION,
    )
    if not run:
        raise RollupStale(f"no transit run is recorded for {scope}; derive transits first")
    run_id, run_batches = run[0][0], set(run[0][1])
    missing = sorted(set(committed) - run_batches)  # a failed batch has no positions
    if missing:
        raise RollupStale(
            f"the latest transit run for {scope} does not include {len(missing)} batch(es) "
            f"of this day, e.g. {missing[0]}"
        )
    counts = conn.run(
        "SELECT count_id::text, interval_start, interval_end, state::text, inbound, outbound, "
        "total, reason FROM eye.run_count WHERE run_id = :r AND interval_start >= :b "
        "AND interval_end <= :e ORDER BY interval_start, interval_end",
        r=run_id,
        b=partition.start,
        e=partition.end,
    )
    by_hour = {(_utc(r[1]), _utc(r[2])): r for r in counts}
    parts = head + [f"run:{run_id}"] + [f"count:{r[0]}:{r[3]}:{r[4]}:{r[5]}:{r[6]}" for r in counts]
    rows: dict[str, tuple] = {}
    hours = []
    for key, start, end in _hours(partition):
        row = by_hour.get((start, end))
        if row is None:
            rows[key] = (
                iso(start),
                iso(end),
                "transits",
                "unknown",
                None,
                "no transit count was derived for this hour",
                {"run_id": run_id},
            )
            hours.append(("unknown", None))
            continue
        _, _, _, state, inbound, outbound, total, reason = row
        detail = {"run_id": run_id, "count_id": row[0]}
        if state in ("qualified", "partial"):
            detail.update(inbound=inbound, outbound=outbound)
        rows[key] = (iso(start), iso(end), "transits", state, total, reason, detail)
        hours.append((state, total))
    unknown = sum(1 for h in hours if h[0] in ("unknown", "failed"))
    if unknown:
        rows["day"] = (
            iso(partition.start),
            iso(partition.end),
            "transits",
            "unknown",
            None,
            f"{unknown} hour(s) without a known count",
            {"run_id": run_id},
        )
    else:
        partial = sum(1 for h in hours if h[0] == "partial")
        state = "partial" if partial else "qualified"
        reason = f"lower bound: {partial} hour(s) are lower bounds" if partial else None
        rows["day"] = (
            iso(partition.start),
            iso(partition.end),
            "transits",
            state,
            sum(h[1] for h in hours),
            reason,
            {"run_id": run_id},
        )
    return Rollup(
        partition,
        TRANSIT,
        scope,
        tuple(batch_ids),
        len(counts),
        _digest(parts),
        watermark,
        rows,
        summary,
        transit_run_id=run_id,
    )


# --- storage ----------------------------------------------------------------------


def store(conn: Connection, rollup: Rollup, lateness_hours: int) -> tuple[str, bool]:
    """Record a manifest and its rows. Idempotent: unchanged inputs give the same id."""
    p = rollup.partition
    required_versions = {rollup.derivation: rollup.version}
    if rollup.derivation == TRANSIT:
        required_versions["transit-counter"] = transits.ALGORITHM_VERSION
    if rollup.derivation in (COVERAGE, OBSERVATIONS, CELLS, LEDGER):
        required_versions["coverage"] = capture.DERIVATION_VERSION
    manifest_id = rollup.manifest_id
    with transaction(conn):
        created = conn.run(
            "INSERT INTO eye.derivation_manifest (manifest_id, source_id, layer, day, "
            "interval_start, interval_end, derivation, derivation_version, scope, "
            "required_versions, input_batch_ids, input_batch_count, input_row_count, "
            "input_sha256, watermark, lateness_deadline, transit_run_id, output_row_count, "
            "output_sha256, coverage_summary) VALUES (:id, :s, :l, :day, :b, :e, :d, :v, "
            ":scope, CAST(:req AS jsonb), CAST(:batches AS uuid[]), :nb, :nr, :isha, :wm, "
            ":deadline, CAST(:run AS uuid), :no, :osha, CAST(:summary AS jsonb)) "
            "ON CONFLICT (manifest_id) DO NOTHING RETURNING manifest_id",
            id=manifest_id,
            s=p.source_id,
            l=p.layer,
            day=p.day,
            b=p.start,
            e=p.end,
            d=rollup.derivation,
            v=rollup.version,
            scope=rollup.scope,
            req=json.dumps(required_versions, sort_keys=True),
            batches=list(rollup.input_batch_ids),
            nb=len(rollup.input_batch_ids),
            nr=rollup.input_row_count,
            isha=rollup.input_sha256,
            wm=rollup.watermark,
            deadline=p.end + timedelta(hours=lateness_hours),
            run=rollup.transit_run_id,
            no=len(rollup.rows),
            osha=rollup.output_sha256,
            summary=json.dumps(rollup.coverage_summary, sort_keys=True),
        )
        if created:
            for key, (start, end, metric, state, value, reason, detail) in rollup.rows.items():
                conn.run(
                    "INSERT INTO eye.rollup_value (manifest_id, row_key, interval_start, "
                    "interval_end, metric, state, value, reason, detail) VALUES (:id, :k, "
                    "CAST(:b AS timestamptz), CAST(:e AS timestamptz), :m, "
                    "CAST(:st AS eye.coverage_state), :val, :r, CAST(:detail AS jsonb))",
                    id=manifest_id,
                    k=key,
                    b=start,
                    e=end,
                    m=metric,
                    st=state,
                    val=None if value is None else Decimal(str(num(value))),
                    r=reason,
                    detail=json.dumps(_canonical_detail(detail), sort_keys=True),
                )
    return manifest_id, bool(created)


def partitions(conn: Connection) -> list[Partition]:
    """Every partition that holds a capture batch (by the day of its requested start)."""
    return [
        Partition(s, layer, d)
        for s, layer, d in conn.run(
            "SELECT DISTINCT source_id, layer, (requested_start AT TIME ZONE 'UTC')::date "
            "FROM eye.capture_batch ORDER BY 1, 2, 3"
        )
    ]


def days_touched(conn: Connection, partition: Partition) -> list[date]:
    """Days overlapped by the requests of the partition's batches (its rollup days)."""
    days = {partition.day}
    for start, end in conn.run(
        "SELECT requested_start, requested_end FROM eye.capture_batch WHERE source_id = :s "
        "AND layer = :l AND (requested_start AT TIME ZONE 'UTC')::date = :d",
        s=partition.source_id,
        l=partition.layer,
        d=partition.day,
    ):
        day = _utc(start).date()
        last = (_utc(end) - timedelta(microseconds=1)).date()
        while day <= last:
            days.add(day)
            day += DAY
    return sorted(days)


def ensure_transit_runs(conn: Connection, partition: Partition, lines) -> None:
    """Bring each line's transit run up to date when it misses this day's batches."""
    if partition.source_id != transits.SOURCE_ID or partition.layer != "vessel":
        return
    for line in lines:
        try:
            with snapshot(conn):
                compute(conn, partition, TRANSIT, line_scope(line), lines)
        except RollupStale:
            inputs = transits.read_inputs(conn, transits.SOURCE_ID, line)
            transits.store(conn, transits.SOURCE_ID, line, transits.hourly_intervals(inputs))


@dataclass(frozen=True)
class RefreshResult:
    partition: Partition
    derivation: str
    scope: str
    manifest_id: str | None
    created: bool
    error: str | None


def refresh(
    conn: Connection,
    partition: Partition,
    lines: list[transits.CountLine],
    lateness_hours: int = DEFAULT_LATENESS_HOURS,
    only: list[tuple[str, str]] | None = None,
) -> list[RefreshResult]:
    """Compute and record every required derivation of one partition day."""
    ensure_transit_runs(conn, partition, lines)
    out = []
    for derivation, scope in only or required(partition, lines):
        try:
            with snapshot(conn):
                rollup = compute(conn, partition, derivation, scope, lines)
        except RollupRefused as exc:
            out.append(RefreshResult(partition, derivation, scope, None, False, str(exc)))
            continue
        except capture.EvidencePruned as exc:
            # Pruned bytes cannot be replayed; the manifest recorded before
            # pruning (and cited by its decision) stays current.
            kept = current_manifest(conn, partition, derivation, scope)
            error = None if kept else str(exc)
            out.append(RefreshResult(partition, derivation, scope, kept and kept[0], False, error))
            continue
        manifest_id, created = store(conn, rollup, lateness_hours)
        out.append(RefreshResult(partition, derivation, scope, manifest_id, created, None))
    return out


def refresh_all(conn, lines, lateness_hours=DEFAULT_LATENESS_HOURS) -> list[RefreshResult]:
    out = []
    for partition in partitions(conn):
        for day in days_touched(conn, partition):
            out += refresh(
                conn, Partition(partition.source_id, partition.layer, day), lines, lateness_hours
            )
    return sorted(set(out), key=lambda r: (r.partition, r.derivation, r.scope))


# --- validation -------------------------------------------------------------------


def stored_rows(conn: Connection, manifest_id: str) -> dict[str, tuple]:
    return {
        key: (
            iso(_utc(start)),
            iso(_utc(end)),
            metric,
            state,
            value,
            reason,
            json.loads(detail) if isinstance(detail, str) else detail,
        )
        for key, start, end, metric, state, value, reason, detail in conn.run(
            "SELECT row_key, interval_start, interval_end, metric, state::text, value, reason, "
            "detail FROM eye.rollup_value WHERE manifest_id = :id",
            id=manifest_id,
        )
    }


def current_manifest(conn, partition: Partition, derivation: str, scope: str):
    rows = conn.run(
        "SELECT manifest_id::text, derivation_version, input_sha256, output_sha256, "
        "output_row_count, transit_run_id::text FROM eye.current_manifest WHERE source_id = :s "
        "AND layer = :l AND day = :d AND derivation = :der AND scope = :scope",
        s=partition.source_id,
        l=partition.layer,
        d=partition.day,
        der=derivation,
        scope=scope,
    )
    return rows[0] if rows else None


def check(conn, partition: Partition, derivation: str, scope: str, lines) -> ManifestCheck:
    """Re-derive one derivation from current evidence and compare with its manifest."""

    def result(manifest_id, state, reason=None):
        return ManifestCheck(partition, derivation, scope, manifest_id, state, reason)

    try:
        latest = current_manifest(conn, partition, derivation, scope)
        if latest is None:
            return result(None, "failed", "no manifest is recorded")
        manifest_id, version, input_sha, output_sha, n_rows, _run = latest
        if version != VERSIONS[derivation]:
            return result(manifest_id, "stale", f"derivation version {version} is not current")
        try:
            fresh = compute(conn, partition, derivation, scope, lines)
        except RollupStale as exc:
            return result(manifest_id, "stale", str(exc))
        except capture.EvidencePruned as exc:
            return result(
                manifest_id,
                "unverified",
                f"{exc}; only the restore drill can re-derive it from the backup",
            )
        except RollupRefused as exc:
            return result(manifest_id, "failed", f"re-derivation refused: {exc}")
        if fresh.input_sha256 != input_sha:
            return result(
                manifest_id,
                "stale",
                f"inputs changed since the manifest ({len(fresh.input_batch_ids)} batch(es) now); "
                "a late arrival or correction needs a new manifest",
            )
        if fresh.output_sha256 != output_sha:
            return result(manifest_id, "failed", "re-derived outputs differ from the manifest")
        stored = stored_rows(conn, manifest_id)
        if len(stored) != n_rows or output_checksum(stored) != output_sha:
            return result(manifest_id, "failed", "stored rollup rows differ from the manifest")
        if derivation == TRANSIT:
            (line,) = [x for x in lines if line_scope(x) == scope]
            problems = transits.verify(conn, partition.source_id, line)
            problems += transits.audit_run(conn, fresh.transit_run_id, line)
            if problems:
                return result(manifest_id, "failed", f"transit replay: {problems[0]}"[:500])
        return result(manifest_id, "valid")
    except (DatabaseError, InterfaceError, OSError) as exc:
        return result(None, "unverified", f"check could not run: {type(exc).__name__}")


def validate(
    conn, partition: Partition, lines, *, own_snapshot: bool = True, ledger: bool = True
) -> list[ManifestCheck]:
    """Check every required derivation of one partition day. Reads only.

    With ``own_snapshot`` false the caller's transaction is used (the retention
    coordinator rechecks inside its locked transaction). With ``ledger`` false
    the ledger replay is skipped (a neighbouring day whose own batches are not
    being pruned).
    """
    try:
        needed = [n for n in required(partition, lines) if ledger or n[0] != LEDGER]
    except RollupRefused as exc:
        return [ManifestCheck(partition, "-", "", None, "failed", str(exc))]
    if partition.source_id == transits.SOURCE_ID and partition.layer == "vessel" and not lines:
        # Transit counts are this source's purpose; never prune without them.
        reason = "no count line is configured, so no transit count can be checked"
        return [ManifestCheck(partition, TRANSIT, "", None, "failed", reason)]
    if not own_snapshot:
        return [check(conn, partition, d, s, lines) for d, s in needed]
    with snapshot(conn):
        return [check(conn, partition, d, s, lines) for d, s in needed]


def record_checks(conn: Connection, checks: list[ManifestCheck]) -> None:
    """Append each check that names a manifest to eye.manifest_validation (caller's txn)."""
    for c in checks:
        if c.manifest_id is not None:
            conn.run(
                "INSERT INTO eye.manifest_validation (manifest_id, state, reason) "
                "VALUES (:id, CAST(:st AS eye.manifest_state), :r)",
                id=c.manifest_id,
                st=c.state,
                r=None if c.state == "valid" else (c.reason or c.state)[:500],
            )
