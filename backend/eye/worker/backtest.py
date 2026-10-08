"""Backtests of coverage-weighted baselines over recorded rollups (Package 5).

A backtest reads hourly rollups only through their manifests: the series'
own derivation (``observations-hourly`` or ``transit-daily``) and the
``coverage-hourly`` manifest of each partition day. Before it computes
anything it re-derives every manifest it is about to cite (``rollups.check``);
a stale manifest (a late arrival or correction not yet rolled up) refuses the
run, so a correction can change a result only through a new manifest and then
a new, recorded run.

Checking and recording share one transaction that first locks the batch,
evidence, transit-run and manifest tables in SHARE mode, so no batch can
arrive or settle and no manifest can be added between the check and the
commit (finding O70). The database guards this independently: a run may cite
only manifests that are still current and complete (migration 0008).

A run is recorded append-only with the exact manifest ids it read, its
parameters and an output checksum. Its id is derived from those, so running
again on the same manifests is a no-op. ``replay`` re-reads the cited
manifests (never the current ones), recomputes, and compares every stored
result.

Bounds: the target range is at most ``max_days`` days, a run has at most
``max_output_rows`` results, and the history read is at most ``history_days``
days before the range; anything larger is refused, never cut short.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from pg8000.exceptions import DatabaseError, InterfaceError

from eye.ingest.capture import stable_id
from eye.storage.db import Connection, transaction
from eye.worker import baselines, rollups
from eye.worker.baselines import BaselineRefused, Hour, Params, Result

SERIES_DERIVATIONS = {rollups.OBSERVATIONS: "observed_states", rollups.TRANSIT: "transits"}
DAY = timedelta(days=1)


class BacktestRefused(BaselineRefused):
    """The backtest cannot run within its bounds or from current, valid manifests."""


@dataclass(frozen=True)
class Series:
    """Which rollup a backtest reads: SOURCE:LAYER:DERIVATION[:SCOPE]."""

    source_id: str
    layer: str
    derivation: str
    scope: str = ""

    @property
    def metric(self) -> str:
        return SERIES_DERIVATIONS[self.derivation]

    @property
    def key(self) -> str:
        tail = f":{self.scope}" if self.scope else ""
        return f"{self.source_id}:{self.layer}:{self.derivation}{tail}"

    @classmethod
    def parse(cls, key: str) -> Series:
        parts = key.split(":", 3)
        if len(parts) < 3:
            raise BacktestRefused("a series is SOURCE:LAYER:DERIVATION[:SCOPE]")
        source_id, layer, derivation = parts[:3]
        scope = parts[3] if len(parts) == 4 else ""
        try:
            rollups.Partition.parse(f"{source_id}:{layer}:2000-01-01")
        except ValueError as exc:
            raise BacktestRefused(str(exc)) from exc
        if not rollups.is_position_source(source_id):
            raise BacktestRefused(f"{source_id} has no hourly position rollups to backtest")
        if derivation not in SERIES_DERIVATIONS:
            raise BacktestRefused(f"a backtest reads one of {sorted(SERIES_DERIVATIONS)}")
        if (derivation == rollups.TRANSIT) != scope.startswith("line:"):
            raise BacktestRefused("transit-daily needs a line:<id>/v<n> scope; others none")
        return cls(source_id, layer, derivation, scope)


@dataclass(frozen=True)
class Limits:
    max_days: int = 92
    max_output_rows: int = 10_000


@dataclass
class Run:
    run_id: str
    series: Series
    first_day: date
    last_day: date
    params: Params
    input_manifest_ids: tuple[str, ...]
    input_sha256: str
    results: list[Result] = field(default_factory=list)
    created: bool = False

    @property
    def output_sha256(self) -> str:
        return output_checksum([baselines.canonical(r) for r in self.results])

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "algorithm_version": baselines.ALGORITHM_VERSION,
            "series": self.series.key,
            "first_day": self.first_day.isoformat(),
            "last_day": self.last_day.isoformat(),
            "params": self.params.as_dict(),
            "input_manifests": len(self.input_manifest_ids),
            "output_sha256": self.output_sha256,
            "created": self.created,
            "summary": baselines.summary(self.results),
            "detected": [
                {
                    "bin": r.row_key,
                    "observed": r.observed,
                    "expected": str(baselines.fixed(r.expected)),
                    "score": str(r.score),
                }
                for r in self.results
                if r.verdict == "detected"
            ],
        }


@dataclass(frozen=True)
class ReplayReport:
    run_id: str
    identical: bool
    differences: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "identical": self.identical,
            "differences": list(self.differences),
        }


def output_checksum(canonical_rows: list) -> str:
    text = json.dumps(canonical_rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def check_bounds(first_day: date, last_day: date, params: Params, limits: Limits) -> None:
    if last_day < first_day:
        raise BacktestRefused("the backtest range ends before it starts")
    days = (last_day - first_day).days + 1
    if days > limits.max_days:
        raise BacktestRefused(
            f"{days} target days exceed research.max_days = {limits.max_days}; refused"
        )
    rows = days * (24 // params.bin_hours)
    if rows > limits.max_output_rows:
        raise BacktestRefused(
            f"{rows} results exceed research.max_output_rows = {limits.max_output_rows}; refused"
        )


def history_start(first_day: date, params: Params) -> date:
    return first_day - params.history_days * DAY


def resolve_inputs(conn: Connection, series: Series, start: date, end: date) -> list[tuple]:
    """Current manifests of the series and of coverage for every day in [start, end]."""
    return [
        (str(mid), derivation, day, scope)
        for mid, derivation, day, scope in conn.run(
            "SELECT manifest_id::text, derivation, day, scope FROM eye.current_manifest "
            "WHERE source_id = :s AND layer = :l AND day BETWEEN :a AND :b AND ("
            "(derivation = :d AND scope = :scope) OR (derivation = :cov AND scope = '')) "
            "ORDER BY day, derivation, manifest_id",
            s=series.source_id,
            l=series.layer,
            a=start,
            b=end,
            d=series.derivation,
            scope=series.scope,
            cov=rollups.COVERAGE,
        )
    ]


def load_series(
    conn: Connection, series: Series, manifest_ids: list[str], start: date, end: date
) -> tuple[dict[datetime, Hour], str]:
    """The hourly series and input checksum from exactly these manifests.

    Refuses a manifest of another series, outside the range, or a second
    manifest for the same day and derivation. A day without a manifest is
    missing (every hour abstains), never zero.
    """
    if len(manifest_ids) != len(set(manifest_ids)):
        raise BacktestRefused("a manifest is cited twice")
    rows = conn.run(
        "SELECT manifest_id::text, source_id, layer, day, derivation, scope, derivation_version, "
        "output_sha256 FROM eye.derivation_manifest "
        "WHERE manifest_id = ANY(CAST(:ids AS uuid[])) ORDER BY manifest_id",
        ids=list(manifest_ids),
    )
    if len(rows) != len(manifest_ids):
        raise BacktestRefused("a cited manifest does not exist")
    by_day: dict[tuple[date, str], str] = {}
    digest = [f"{baselines.ALGORITHM_VERSION}:{series.key}"]
    for mid, source_id, layer, day, derivation, scope, version, output_sha in rows:
        wanted = (derivation == series.derivation and scope == series.scope) or (
            derivation == rollups.COVERAGE and scope == ""
        )
        if (source_id, layer) != (series.source_id, series.layer) or not wanted:
            raise BacktestRefused(f"manifest {mid} belongs to another series")
        if not start <= day <= end:
            raise BacktestRefused(f"manifest {mid} is outside the backtest's days")
        if version != rollups.VERSIONS[derivation]:
            raise BacktestRefused(f"manifest {mid} has derivation version {version}")
        if (day, derivation) in by_day:
            raise BacktestRefused(f"two manifests for {derivation} on {day}")
        by_day[(day, derivation)] = mid
        digest.append(f"{mid}:{derivation}:{day.isoformat()}:{output_sha}")
    values = conn.run(
        "SELECT manifest_id::text, row_key, interval_start, state::text, value, detail "
        "FROM eye.rollup_value WHERE manifest_id = ANY(CAST(:ids AS uuid[])) "
        "AND row_key LIKE 'hour:%' ORDER BY manifest_id, row_key",
        ids=list(manifest_ids),
    )
    by_manifest: dict[str, dict[datetime, tuple]] = {}
    for mid, _key, start_at, state, value, detail in values:
        detail = json.loads(detail) if isinstance(detail, str) else detail
        by_manifest.setdefault(mid, {})[_utc(start_at)] = (state, value, detail)
    out: dict[datetime, Hour] = {}
    day = start
    while day <= end:
        metric = by_manifest.get(by_day.get((day, series.derivation), ""), {})
        cover = by_manifest.get(by_day.get((day, rollups.COVERAGE), ""), {})
        for h in range(24):
            moment = baselines.day_start(day) + h * baselines.HOUR
            m = metric.get(moment)
            c = cover.get(moment)
            qualified = None if c is None else Fraction(Decimal(str(c[2]["qualified_s"])))
            if m is None:
                out[moment] = Hour(moment, "missing", None, qualified)
                continue
            state, value, _ = m
            exact = None if value is None else int(value)
            if value is not None and Decimal(value) != exact:
                raise BacktestRefused(f"a {series.metric} value is not a whole number")
            out[moment] = Hour(moment, state, exact, qualified)
        day += DAY
    return out, hashlib.sha256("\n".join(digest).encode()).hexdigest()


def run_id_for(series: Series, first: date, last: date, params: Params, input_sha: str) -> str:
    return stable_id(
        "backtest",
        baselines.ALGORITHM_VERSION,
        series.key,
        first.isoformat(),
        last.isoformat(),
        json.dumps(params.as_dict(), sort_keys=True),
        input_sha,
    )


LOCKED_TABLES = (
    "eye.capture_batch",
    "eye.raw_evidence",
    "eye.derivation_run",
    "eye.derivation_manifest",
)


@contextlib.contextmanager
def locked(conn: Connection) -> Iterator[Connection]:
    """One transaction in which no batch can arrive or settle and no manifest or
    transit run can be added until it ends.

    SHARE mode lets other readers (and other backtests) proceed but makes every
    writer to these tables wait for this transaction's commit, so the manifests
    checked inside it cannot go stale before the run is recorded (finding O70).

    READ COMMITTED, set explicitly whatever the session default, so every check
    after the lock sees all that committed before it; the database guard refuses
    any other isolation level.
    """
    with transaction(conn):
        conn.run("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        conn.run(f"LOCK TABLE {', '.join(LOCKED_TABLES)} IN SHARE MODE")
        yield conn


def _evaluate(conn, series, first_day, last_day, params, lines) -> Run:
    """Resolve and check the current manifests, read them and backtest (caller's transaction)."""
    start = history_start(first_day, params)
    inputs = resolve_inputs(conn, series, start, last_day)
    problems = []
    for mid, derivation, day, scope in inputs:
        partition = rollups.Partition(series.source_id, series.layer, day)
        checked = rollups.check(conn, partition, derivation, scope, lines)
        if checked.manifest_id != mid or checked.state != "valid":
            problems.append(f"{checked.label}: {checked.state} ({checked.reason})")
    if problems:
        raise BacktestRefused(
            f"{len(problems)} cited manifest(s) are not valid now; run db-rollup for a "
            f"new manifest first, e.g. {problems[0]}"[:500]
        )
    ids = [i[0] for i in inputs]
    hours, input_sha = load_series(conn, series, ids, start, last_day)
    results = baselines.backtest(hours, first_day, last_day, params)
    run_id = run_id_for(series, first_day, last_day, params, input_sha)
    return Run(run_id, series, first_day, last_day, params, tuple(sorted(ids)), input_sha, results)


def compute(
    conn: Connection,
    series: Series,
    first_day: date,
    last_day: date,
    params: Params,
    limits: Limits,
    lines,
) -> Run:
    """A read-only preview: resolve, check and read the current manifests, then backtest.

    It records nothing. ``store`` re-derives it under the lock before recording.
    """
    check_bounds(first_day, last_day, params, limits)
    with rollups.snapshot(conn):
        return _evaluate(conn, series, first_day, last_day, params, lines)


def _insert(conn: Connection, run: Run) -> bool:
    counts = baselines.summary(run.results)
    created = conn.run(
        "INSERT INTO eye.backtest_run (run_id, algorithm_version, source_id, layer, "
        "derivation, scope, metric, first_day, last_day, params, input_manifest_ids, "
        "input_sha256, output_row_count, output_sha256, detected, not_detected, abstained) "
        "VALUES (:id, :alg, :s, :l, :d, :scope, :m, :a, :b, CAST(:params AS jsonb), "
        "CAST(:ids AS uuid[]), :isha, :n, :osha, :det, :nd, :ab) "
        "ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
        id=run.run_id,
        alg=baselines.ALGORITHM_VERSION,
        s=run.series.source_id,
        l=run.series.layer,
        d=run.series.derivation,
        scope=run.series.scope,
        m=run.series.metric,
        a=run.first_day,
        b=run.last_day,
        params=json.dumps(run.params.as_dict(), sort_keys=True),
        ids=list(run.input_manifest_ids),
        isha=run.input_sha256,
        n=len(run.results),
        osha=run.output_sha256,
        det=counts["detected"],
        nd=counts["not_detected"],
        ab=counts["abstained"],
    )
    if created:
        for r in run.results:
            conn.run(
                "INSERT INTO eye.backtest_result (run_id, row_key, bin_start, bin_end, "
                "verdict, reason_code, observed, exposure_s, expected, score, detail) "
                "VALUES (:id, :k, :b, :e, :v, :rc, :o, :x, :ex, :sc, CAST(:detail AS jsonb))",
                id=run.run_id,
                k=r.row_key,
                b=r.bin_start,
                e=r.bin_end,
                v=r.verdict,
                rc=r.reason_code,
                o=r.observed,
                x=r.exposure_s,
                ex=None if r.expected is None else baselines.fixed(r.expected),
                sc=r.score,
                detail=json.dumps(r.detail, sort_keys=True),
            )
    return bool(created)


def store(conn: Connection, run: Run, lines=()) -> Run:
    """Record a computed run, re-deriving it under the lock first.

    The manifests are re-resolved and re-checked, and the results recomputed,
    inside the same locked transaction that inserts them. A run whose inputs
    changed since it was computed (a late arrival, a correction, a new
    manifest) is refused and nothing is recorded. Idempotent.
    """
    with locked(conn):
        fresh = _evaluate(conn, run.series, run.first_day, run.last_day, run.params, lines)
        if fresh.run_id != run.run_id or fresh.output_sha256 != run.output_sha256:
            raise BacktestRefused(
                "the cited manifests or their inputs changed after this run was computed; "
                "nothing is recorded"
            )
        run.created = _insert(conn, fresh)
    return run


def run_backtest(conn, series, first_day, last_day, params, limits, lines) -> Run:
    """Check, read, backtest and record in one locked transaction."""
    check_bounds(first_day, last_day, params, limits)
    with locked(conn):
        run = _evaluate(conn, series, first_day, last_day, params, lines)
        run.created = _insert(conn, run)
    return run


def stored_canonical(conn: Connection, run_id: str) -> list:
    rows = conn.run(
        "SELECT row_key, bin_start, bin_end, verdict, reason_code, observed, exposure_s, "
        "expected, score, detail FROM eye.backtest_result WHERE run_id = :id ORDER BY bin_start",
        id=run_id,
    )
    out = []
    for key, b, e, verdict, reason, observed, exposure, expected, score, detail in rows:
        detail = json.loads(detail) if isinstance(detail, str) else detail
        out.append(
            [
                key,
                _utc(b).isoformat(),
                _utc(e).isoformat(),
                verdict,
                reason,
                None if observed is None else int(observed),
                int(exposure),
                None if expected is None else str(baselines.fixed(Decimal(expected))),
                None if score is None else str(baselines.fixed(Decimal(score))),
                {k: detail[k] for k in sorted(detail)},
            ]
        )
    return out


def replay(conn: Connection, run_id: str) -> ReplayReport:
    """Recompute a recorded run from the manifests it cites and compare every result."""
    try:
        with rollups.snapshot(conn):
            found = conn.run(
                "SELECT source_id, layer, derivation, scope, first_day, last_day, params, "
                "input_manifest_ids::text[], input_sha256, output_sha256, algorithm_version "
                "FROM eye.backtest_run WHERE run_id = :id",
                id=run_id,
            )
            if not found:
                return ReplayReport(run_id, False, ("no such backtest run is recorded",))
            s, layer, derivation, scope, first, last, params, ids, isha, osha, alg = found[0]
            if alg != baselines.ALGORITHM_VERSION:
                return ReplayReport(run_id, False, (f"recorded with {alg}",))
            params = Params(**(json.loads(params) if isinstance(params, str) else params))
            series = Series(s, layer, derivation, scope)
            start = history_start(first, params)
            hours, input_sha = load_series(conn, series, list(ids), start, last)
            stored = stored_canonical(conn, run_id)
    except (DatabaseError, InterfaceError) as exc:
        reason = f"UNVERIFIED: replay could not run ({type(exc).__name__})"
        return ReplayReport(run_id, False, (reason,))
    fresh = [baselines.canonical(r) for r in baselines.backtest(hours, first, last, params)]
    differences = []
    if input_sha != isha:
        differences.append("the cited manifests no longer give the recorded input checksum")
    if run_id_for(series, first, last, params, input_sha) != run_id:
        differences.append("the run id does not follow from its recorded inputs")
    if output_checksum(fresh) != osha:
        differences.append("recomputed results differ from the recorded output checksum")
    if output_checksum(stored) != osha:
        differences.append("stored result rows differ from the recorded output checksum")
    stored_by_key = {row[0]: row for row in stored}
    for row in fresh:
        if stored_by_key.get(row[0]) != row:
            differences.append(f"{row[0]}: stored {stored_by_key.get(row[0])} recomputed {row}")
    return ReplayReport(run_id, not differences, tuple(d[:300] for d in differences[:20]))
