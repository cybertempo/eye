"""Package 5 research: coverage-weighted baselines and the backtester.

Five controls, each beside a successful control:

* abstention: missing or unknown coverage abstains, never counts as zero;
* planted signal: the signal planted in the fixture is detected, at its bin only;
* shuffled and null data: no false detection, while almost every bin is evaluated;
* late corrections: a late arrival changes a result only through a new manifest
  and a new recorded run; the old run stays and still replays;
* replay: a run replayed from the same manifests gives identical results.

Where a control could pass vacuously, a mutant (zero-filling gaps, reading the
current rollups instead of the cited manifests, an altered stored result)
shows the same assertion would fail. Fixtures are invented
(``tests/fixtures/synthetic/research/series.v1.json``) and go through the real
capture pipeline and rollups.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import subprocess
import sys
import uuid
from datetime import date, timedelta
from fractions import Fraction
from urllib.parse import urlsplit, urlunsplit

import pytest
import synthetic_research as sr
from conftest import EXAMPLE_CONFIG, REPO_ROOT
from eye.config import ConfigError, parse_config
from eye.ingest.capture import ingest, load_fixtures
from eye.storage.db import connect, transaction
from eye.storage.migrate import migrate
from eye.worker import backtest, baselines, rollups, transits
from eye.worker.backtest import BacktestRefused, Limits, Series
from eye.worker.baselines import Hour, Params
from pg8000.exceptions import DatabaseError

SPEC_PATH = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "research" / "series.v1.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
SERIES = Series.parse("synthetic-fixture:vessel:observations-hourly")
FIRST, LAST = date(2026, 3, 15), date(2026, 3, 22)
PLANTED_BIN = "day:2026-03-22:bin:13"
LINES = [transits.load_line(p) for p in sorted((REPO_ROOT / "reference" / "lines").glob("*.json"))]


def key(day: str, hour: int) -> str:
    return f"day:{day}:bin:{hour:02d}"


def run(conn, params: Params | None = None, first=FIRST, last=LAST) -> backtest.Run:
    return backtest.run_backtest(conn, SERIES, first, last, params or Params(), Limits(), [])


def by_key(results) -> dict:
    return {r.row_key: r for r in results}


def detected(results) -> list[str]:
    return [r.row_key for r in results if r.verdict == "detected"]


# --- database scenarios ------------------------------------------------------------


@pytest.fixture(scope="module")
def scenario_db(admin_url):
    """One disposable database per fixture scenario, loaded and rolled up once."""
    admin = connect(admin_url)
    made: list[str] = []
    conns = []

    def make(scenario: str | None, *, rollup: bool = True):
        name = f"eye_r_{uuid.uuid4().hex[:12]}"
        admin.run(f'CREATE DATABASE "{name}"')
        made.append(name)
        url = urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
        conn = connect(url)
        conn.url = url
        conns.append(conn)
        migrate(conn)
        if scenario is not None:
            for doc in sr.captures(SPEC, scenario):
                ingest(conn, doc)
            if rollup:
                assert not [r for r in rollups.refresh_all(conn, []) if r.error]
        return conn

    cache: dict[str, object] = {}

    def get(scenario: str):
        if scenario not in cache:
            cache[scenario] = make(scenario)
        return cache[scenario]

    get.fresh = make
    yield get
    for conn in conns:
        with contextlib.suppress(Exception):  # the database is dropped next
            conn.close()
    for name in made:
        admin.run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


# --- the fixture itself ------------------------------------------------------------


def test_fixture_is_regenerated_exactly_and_plants_one_signal():
    assert sr.build_spec() == SPEC  # every committed count follows from its seeds
    null, planted = SPEC["scenarios"]["null"]["counts"], SPEC["scenarios"]["planted"]["counts"]
    differ = [
        (d, h, planted[d][h] - null[d][h])
        for d in range(len(null))
        for h in range(24)
        if planted[d][h] != null[d][h]
    ]
    assert differ == [(21, 13, 20)]  # 2026-03-22 13:00, 20 extra observed states
    shuffled = SPEC["scenarios"]["shuffled"]["counts"]
    assert sorted(v for r in shuffled for v in r) == sorted(v for r in null for v in r)
    assert shuffled != null


# --- control 1: abstention ---------------------------------------------------------


def series_of(counts: dict[int, list[int]], first=date(2026, 3, 1), states=None) -> dict:
    """An in-memory series: day index -> 24 counts; ``states`` overrides (day, hour)."""
    out = {}
    for d, row in counts.items():
        for h, v in enumerate(row):
            t = baselines.day_start(first + timedelta(days=d)) + h * baselines.HOUR
            state = (states or {}).get((d, h), "qualified")
            qualified = Fraction(3600) if state == "qualified" else Fraction(0)
            value = None if state in ("unknown", "failed") else v
            out[t] = Hour(t, state, value, qualified)
    return out


def test_unknown_partial_or_missing_target_abstains_and_never_counts_as_zero():
    history = {d: [3] * 24 for d in range(14)}
    target = 14
    for state in ("unknown", "failed", "partial", "missing"):
        series = series_of({**history, target: [0] * 24}, states={(target, 5): state})
        if state == "missing":
            del series[baselines.day_start(date(2026, 3, 15)) + 5 * baselines.HOUR]
        result = baselines.evaluate_bin(series, date(2026, 3, 15), 5, Params())
        assert (result.verdict, result.reason_code) == ("abstained", "target_not_covered")
        assert (result.observed, result.expected, result.score) == (None, None, None)
    # Positive control: the same hour, covered, with nothing seen, is a real 0.
    covered = baselines.evaluate_bin(
        series_of({**history, target: [0] * 24}), date(2026, 3, 15), 5, Params()
    )
    assert covered.verdict == "not_detected" and covered.observed == 0
    assert covered.expected == 3


def test_history_with_too_little_coverage_abstains():
    # Hour 6 failed on 12 of the 14 history days: two exact bins are below the minimum 3.
    states = {(d, 6): "failed" for d in range(12)}
    series = series_of({d: [3] * 24 for d in range(15)}, states=states)
    result = baselines.evaluate_bin(series, date(2026, 3, 15), 6, Params())
    assert (result.verdict, result.reason_code) == ("abstained", "history_insufficient")
    assert result.detail["history_bins_used"] == 2
    # Positive control: three exact bins are enough.
    series = series_of(
        {d: [3] * 24 for d in range(15)}, states={(d, 6): "failed" for d in range(11)}
    )
    assert baselines.evaluate_bin(series, date(2026, 3, 15), 6, Params()).verdict == "not_detected"


def test_baseline_is_weighted_by_exact_coverage():
    """A six-hour bin with three unknown hours is judged on its three covered hours."""
    series = series_of(
        {d: [2] * 24 for d in range(15)},
        states={(14, 18): "unknown", (14, 19): "unknown", (14, 20): "unknown", (3, 22): "failed"},
    )
    half = Params(bin_hours=6, min_target_coverage_pct=50)
    result = baselines.evaluate_bin(series, date(2026, 3, 15), 3, half)
    assert result.verdict == "not_detected"
    assert (result.exposure_s, result.observed, result.expected) == (10800, 6, 6)
    # History day 3 lost one hour: it weighs 5/6 of a bin, not a bin with a zero.
    assert result.detail["history_exposure_s"] == 14 * 21600 - 3600
    # The same bin under a full-coverage requirement abstains.
    full = baselines.evaluate_bin(series, date(2026, 3, 15), 3, Params(bin_hours=6))
    assert (full.verdict, full.reason_code) == ("abstained", "target_not_covered")


def zero_filled(hours: dict) -> dict:
    """Mutant: treat every gap as a measured zero (what the baseline must never do)."""
    return {
        t: Hour(t, "qualified", 0, Fraction(3600)) if h.exposure_s == 0 else h
        for t, h in hours.items()
    }


def test_outage_scenario_abstains_where_zero_filling_would_raise_false_alarms(scenario_db):
    conn = scenario_db("outage")
    result = run(conn)
    rows = by_key(result.results)
    # Hours 06-08 failed on every earlier day: the last day's hours abstain on history,
    # earlier days' on the target itself; the later outage abstains too.
    for h in (6, 7, 8):
        assert rows[key("2026-03-22", h)].reason_code == "history_insufficient"
        for day in range(15, 22):
            assert rows[key(f"2026-03-{day}", h)].reason_code == "target_not_covered"
    for h in (18, 19, 20):
        assert rows[key("2026-03-22", h)].reason_code == "target_not_covered"
    stored = conn.run(
        "SELECT count(*) FROM eye.backtest_result WHERE run_id = :r AND verdict = 'abstained' "
        "AND observed IS NULL AND expected IS NULL AND score IS NULL",
        r=result.run_id,
    )[0][0]
    assert stored == baselines.summary(result.results)["abstained"] == 30
    assert detected(result.results) == []
    # Positive control: hour 9 of the same day is covered and evaluated.
    assert rows[key("2026-03-22", 9)].verdict == "not_detected"
    # The mutant reads the very same manifests but zero-fills the gaps: the quiet
    # "baseline" turns ordinary morning traffic into detections.
    hours, _ = backtest.load_series(
        conn, SERIES, list(result.input_manifest_ids), date(2026, 3, 1), LAST
    )
    mutant = baselines.backtest(zero_filled(hours), FIRST, LAST, Params())
    assert detected(mutant)
    assert all(int(k[-2:]) in (6, 7, 8, 18, 19, 20) for k in detected(mutant))


def test_database_refuses_a_gap_stored_as_zero_or_prose(scenario_db):
    conn = scenario_db("outage")
    run_id = run(conn).run_id
    good = (
        "INSERT INTO eye.backtest_result (run_id, row_key, bin_start, bin_end, verdict, "
        "reason_code, observed, exposure_s, expected, score, detail) VALUES (:r, "
        "'day:2030-01-01:bin:00', '2030-01-01T00:00:00Z', '2030-01-01T01:00:00Z', {v}, {rc}, "
        "{o}, {x}, {e}, {s}, CAST(:d AS jsonb))"
    )
    cases = {
        "abstained with a zero": ("'abstained'", "'target_not_covered'", "0", "0", "NULL", "NULL"),
        "evaluated without exposure": ("'not_detected'", "NULL", "0", "0", "0", "0"),
        "abstained without a reason": ("'abstained'", "NULL", "NULL", "0", "NULL", "NULL"),
    }
    for v, rc, o, x, e, s in cases.values():
        with pytest.raises(DatabaseError), transaction(conn):
            conn.run(good.format(v=v, rc=rc, o=o, x=x, e=e, s=s), r=run_id, d="{}")
    abstained = good.format(
        v="'abstained'", rc="'target_not_covered'", o="NULL", x="0", e="NULL", s="NULL"
    )
    with pytest.raises(DatabaseError), transaction(conn):
        conn.run(abstained, r=run_id, d=json.dumps({"note": "looks like a spike"}))
    # Positive control: a well-formed abstained row is accepted (then rolled back).
    with pytest.raises(RuntimeError), transaction(conn):
        conn.run(abstained, r=run_id, d=json.dumps({"hours_missing": 1}))
        raise RuntimeError("roll back the accepted row")


# --- control 2: planted signal -----------------------------------------------------


def test_planted_signal_is_detected_at_its_bin_only(scenario_db):
    planted = run(scenario_db("planted"))
    assert detected(planted.results) == [PLANTED_BIN]
    hit = by_key(planted.results)[PLANTED_BIN]
    assert hit.observed == 21 and hit.score >= 5
    # Positive control for the negative: the same bin without the plant is not detected.
    plain = by_key(run(scenario_db("null")).results)[PLANTED_BIN]
    assert plain.verdict == "not_detected" and plain.observed == 1


# --- control 3: shuffled and null data ---------------------------------------------


@pytest.mark.parametrize("scenario", ["null", "shuffled"])
def test_null_and_shuffled_fixtures_produce_no_detection(scenario_db, scenario):
    result = run(scenario_db(scenario))
    summary = baselines.summary(result.results)
    assert summary["detected"] == 0
    # Not vacuous: all but the six uncovered hours were evaluated.
    assert summary == {"detected": 0, "not_detected": 186, "abstained": 6}


def poisson_counts(rng: random.Random, days: int) -> list[list[int]]:
    return [[sr._poisson(rng, m) for m in sr.PATTERN] for _ in range(days)]


def test_false_detection_rate_on_many_null_and_shuffled_series_stays_small():
    """A seeded statistical control (pure, no database): false detections per bin."""
    false = evaluated = planted_hits = 0
    for seed in range(60):
        rng = random.Random(1000 + seed)  # noqa: S311 - reproducible synthetic data
        counts = poisson_counts(rng, 22)
        flat = [v for row in counts for v in row]
        rng.shuffle(flat)
        shuffled = [flat[i * 24 : (i + 1) * 24] for i in range(22)]
        for data in (counts, shuffled):
            results = baselines.backtest(series_of(dict(enumerate(data))), FIRST, LAST, Params())
            false += len(detected(results))
            evaluated += sum(r.verdict != "abstained" for r in results)
        counts[21][13] += 20
        hit = baselines.evaluate_bin(series_of(dict(enumerate(counts))), LAST, 13, Params())
        planted_hits += hit.verdict == "detected"
    assert evaluated == 60 * 2 * 192
    assert false / evaluated < 0.002  # observed rate with these seeds is reported in the ADR
    assert planted_hits >= 54  # the planted signal is found in at least 90% of series


# --- control 4: late corrections ---------------------------------------------------


def test_late_arrival_changes_a_result_only_through_a_new_recorded_derivation(scenario_db):
    conn = scenario_db.fresh("null")
    first = run(conn)
    before = by_key(first.results)
    assert before[key("2026-03-22", 19)].reason_code == "target_not_covered"
    for doc in sr.late_captures(SPEC):
        ingest(conn, doc)  # the timed-out block arrives complete, late
    # Negative control: the manifests no longer match the evidence; no run is recorded.
    with pytest.raises(BacktestRefused, match="not valid now"):
        run(conn)
    assert conn.run("SELECT count(*) FROM eye.backtest_run")[0][0] == 1
    # The recorded run is unchanged and still replays from the manifests it cites.
    assert backtest.replay(conn, first.run_id).identical
    # Mutant: reading the current rollups instead of the cited manifests would make
    # the old run's result move after the next rollup.
    assert not [r for r in rollups.refresh_all(conn, []) if r.error]
    current = [i[0] for i in backtest.resolve_inputs(conn, SERIES, date(2026, 3, 1), LAST)]
    hours, _ = backtest.load_series(conn, SERIES, current, date(2026, 3, 1), LAST)
    moved = by_key(baselines.backtest(hours, FIRST, LAST, Params()))
    assert moved[key("2026-03-22", 19)].verdict != before[key("2026-03-22", 19)].verdict
    assert backtest.replay(conn, first.run_id).identical  # pinned: it does not move
    # Positive control: the new manifests give a new, recorded run with the new result.
    second = run(conn)
    after = by_key(second.results)
    assert second.run_id != first.run_id and second.created
    assert set(second.input_manifest_ids) != set(first.input_manifest_ids)
    for h in (18, 19, 20):
        assert after[key("2026-03-22", h)].verdict == "not_detected"
        assert after[key("2026-03-22", h)].observed is not None
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {key("2026-03-22", h) for h in (18, 19, 20)}
    assert conn.run("SELECT count(*) FROM eye.backtest_run")[0][0] == 2
    assert backtest.replay(conn, second.run_id).identical


# --- control 5: replay -------------------------------------------------------------


def test_replay_from_the_same_manifests_gives_identical_results(scenario_db):
    conn = scenario_db("planted")
    first = run(conn)
    again = run(conn)
    assert again.run_id == first.run_id and not again.created  # recorded once
    report = backtest.replay(conn, first.run_id)
    assert report.identical and report.differences == ()
    # A separate database built from the same fixtures reproduces the manifests,
    # the run id and every result.
    other = scenario_db.fresh("planted")
    rebuilt = run(other)
    assert rebuilt.run_id == first.run_id
    assert rebuilt.input_manifest_ids == first.input_manifest_ids
    assert backtest.stored_canonical(other, rebuilt.run_id) == backtest.stored_canonical(
        conn, first.run_id
    )
    # Pure determinism: reversing the series' insertion order changes nothing.
    hours, _ = backtest.load_series(
        conn, SERIES, list(first.input_manifest_ids), date(2026, 3, 1), LAST
    )
    reversed_hours = dict(reversed(list(hours.items())))
    assert [
        baselines.canonical(r) for r in baselines.backtest(reversed_hours, FIRST, LAST, Params())
    ] == [baselines.canonical(r) for r in first.results]


def test_replay_detects_an_altered_result_and_results_are_append_only(scenario_db):
    conn = scenario_db("null")
    result = run(conn, Params(threshold_sigma=6))  # a run of its own, altered below
    with pytest.raises(DatabaseError, match="append-only|refuse"), transaction(conn):
        conn.run(
            "UPDATE eye.backtest_result SET observed = observed + 1 WHERE run_id = :r "
            "AND verdict = 'not_detected'",
            r=result.run_id,
        )
    assert backtest.replay(conn, result.run_id).identical
    # Negative control: with the guard disabled in this disposable database, an
    # altered stored result is caught by replay.
    with transaction(conn):
        conn.run("ALTER TABLE eye.backtest_result DISABLE TRIGGER backtest_result_append_only")
        conn.run(
            "UPDATE eye.backtest_result SET observed = observed + 1 WHERE run_id = :r "
            "AND row_key = :k",
            r=result.run_id,
            k=key("2026-03-20", 9),
        )
        conn.run("ALTER TABLE eye.backtest_result ENABLE TRIGGER backtest_result_append_only")
    report = backtest.replay(conn, result.run_id)
    assert not report.identical
    assert any(key("2026-03-20", 9) in d for d in report.differences)
    assert backtest.replay(conn, str(uuid.uuid4())).differences == (
        "no such backtest run is recorded",
    )


# --- bounds, inputs and other series -----------------------------------------------


def test_bounds_refuse_rather_than_cut_short():
    params = Params()
    backtest.check_bounds(date(2026, 1, 1), date(2026, 4, 2), params, Limits())  # 92 days
    with pytest.raises(BacktestRefused, match="max_days"):
        backtest.check_bounds(date(2026, 1, 1), date(2026, 4, 3), params, Limits())
    backtest.check_bounds(FIRST, LAST, params, Limits(max_output_rows=192))
    with pytest.raises(BacktestRefused, match="max_output_rows"):
        backtest.check_bounds(FIRST, LAST, params, Limits(max_output_rows=191))
    with pytest.raises(BacktestRefused, match="ends before"):
        backtest.check_bounds(LAST, FIRST, params, Limits())
    for bad in ({"bin_hours": 5}, {"history_days": 61}, {"min_history_bins": 15}):
        with pytest.raises(baselines.BaselineRefused):
            Params(**bad)


def test_series_and_cited_manifests_are_checked(scenario_db):
    for bad in (
        "synthetic-fixture:vessel",
        "nope:vessel:observations-hourly",
        "synthetic-fixture:vessel:coverage-hourly",
        "synthetic-ais:vessel:transit-daily",
    ):
        with pytest.raises(BacktestRefused):
            Series.parse(bad)
    conn = scenario_db("null")
    ids = [i[0] for i in backtest.resolve_inputs(conn, SERIES, date(2026, 3, 1), LAST)]
    with pytest.raises(BacktestRefused, match="cited twice"):
        backtest.load_series(conn, SERIES, [*ids, ids[0]], date(2026, 3, 1), LAST)
    with pytest.raises(BacktestRefused, match="outside"):
        backtest.load_series(conn, SERIES, ids, date(2026, 3, 2), LAST)
    with pytest.raises(BacktestRefused, match="does not exist"):
        backtest.load_series(conn, SERIES, [str(uuid.uuid4())], date(2026, 3, 1), LAST)
    # Positive control: the resolved set loads.
    hours, _ = backtest.load_series(conn, SERIES, ids, date(2026, 3, 1), LAST)
    assert len(hours) == 22 * 24


def test_transit_series_with_one_day_of_history_abstains(make_db):
    conn = connect(make_db())
    migrate(conn)
    load_fixtures(conn, REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ais" / "demo")
    for line in LINES:
        inputs = transits.read_inputs(conn, transits.SOURCE_ID, line)
        transits.store(conn, transits.SOURCE_ID, line, transits.hourly_intervals(inputs))
    assert not [r for r in rollups.refresh_all(conn, LINES) if r.error]
    series = Series.parse(f"synthetic-ais:vessel:transit-daily:{rollups.line_scope(LINES[0])}")
    day = date(2026, 2, 1)
    result = backtest.run_backtest(conn, series, day, day, Params(), Limits(), LINES)
    reasons = {r.reason_code for r in result.results}
    assert baselines.summary(result.results)["abstained"] == 24
    assert reasons <= {"history_insufficient", "target_not_covered"}
    assert backtest.replay(conn, result.run_id).identical
    conn.close()


# --- configuration and command line ------------------------------------------------


def test_research_configuration_defaults_and_refusals(example_raw):
    config = parse_config(example_raw, EXAMPLE_CONFIG, {})
    assert config.research.history_days == 14 and config.research.max_days == 92
    for bad in ({"bin_hours": 5}, {"max_days": 400}, {"min_history_bins": 20}, {"x": 1}):
        raw = {**example_raw, "research": {**example_raw["research"], **bad}}
        with pytest.raises(ConfigError):
            parse_config(raw, EXAMPLE_CONFIG, {})


def run_eye(*args: str, url: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "backend"), EYE_DATABASE_URL=url)
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "eye", *args, "--config", str(EXAMPLE_CONFIG)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=REPO_ROOT,
    )


def test_backtest_commands_end_to_end(scenario_db):
    url = scenario_db("planted").url
    assert run_eye("db-rollup", url=url).returncode == 0  # already current: records nothing
    missing = run_eye("db-backtest", url=url)
    assert missing.returncode == 2 and "--series" in missing.stderr
    window = ("db-backtest", "--series", SERIES.key, "--start")
    too_long = run_eye(*window, "2026-01-01", "--end", "2026-12-31", url=url)
    assert too_long.returncode == 2 and "max_days" in too_long.stderr
    done = run_eye(*window, "2026-03-15", "--end", "2026-03-22", url=url)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert [d["bin"] for d in report["detected"]] == [PLANTED_BIN]
    assert report["summary"] == {"detected": 1, "not_detected": 185, "abstained": 6}
    replayed = run_eye("db-backtest-replay", "--run", report["run_id"], url=url)
    assert replayed.returncode == 0 and json.loads(replayed.stdout)["identical"] is True
    unknown = run_eye("db-backtest-replay", "--run", str(uuid.uuid4()), url=url)
    assert unknown.returncode == 5 and json.loads(unknown.stdout)["identical"] is False


def test_no_model_or_network_in_the_research_path():
    for name in ("baselines.py", "backtest.py"):
        text = (REPO_ROOT / "backend" / "eye" / "worker" / name).read_text(encoding="utf-8")
        for forbidden in (
            "import socket",
            "urllib",
            "http.client",
            "import requests",
            "import random",
        ):
            assert forbidden not in text, (name, forbidden)
    # Positive control: the scan does find a forbidden word where one is present.
    assert (
        "import random" in (REPO_ROOT / "tests" / "support" / "synthetic_research.py").read_text()
    )


# --- migration ---------------------------------------------------------------------


def test_migration_0008_leaves_earlier_history_unchanged(make_db, tmp_path):
    import shutil

    migrations = REPO_ROOT / "migrations"
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(migrations.glob("000[1-7]_*.sql")):
        shutil.copy(path, before / path.name)
    conn = connect(make_db())
    assert migrate(conn, before) == [1, 2, 3, 4, 5, 6, 7]
    for doc in sr.captures(SPEC, "null")[:16]:
        ingest(conn, doc)
    assert not [r for r in rollups.refresh_all(conn, []) if r.error]
    tables = (
        "capture_batch",
        "observation",
        "coverage",
        "derivation_manifest",
        "rollup_value",
        "batch_seal",
    )

    def digest():
        return [
            conn.run(
                f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "  # noqa: S608
                f"FROM eye.{name} t"
            )
            for name in tables
        ]

    prior = digest()
    assert migrate(conn) == [8]
    assert digest() == prior
    # Positive control: the new tables accept a run over the old manifests.
    result = backtest.run_backtest(
        conn, SERIES, date(2026, 3, 2), date(2026, 3, 2), Params(), Limits(), []
    )
    assert result.created and backtest.replay(conn, result.run_id).identical
    conn.close()
