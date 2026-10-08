# ADR 0008: Package 5 research slice (coverage-weighted baselines and backtester)

Date: 2026-10-08. Status: proposed (awaiting owner review).

Scope:
- **Built:** coverage-weighted baselines and a backtester over the lifecycle
  rollups of [ADR 0007](0007-package-5-lifecycle.md), recorded backtest runs
  (migration 0008), two commands and invented fixtures with five controls.
- **Not built:** imagery (Package 6); deletion of real data (it still needs
  database role separation and a proven real backup and restore in the private
  environment, Package 8); any wire or browser change. Results are reported by
  command only and the wire schema stays `eye.wire/4`.
- **Data:** invented fixtures only. No provider, account, credential, private
  host or paid request. Migrations 0001 to 0007 are unchanged.
- **No AI:** no model is called or consulted anywhere in this path.

## Decisions

1. **The input is recorded rollups, read through their manifests.** A backtest
   reads one hourly series: `observations-hourly` (position sources) or
   `transit-daily` for one count line (`synthetic-ais` vessels). Coverage comes
   from the `coverage-hourly` manifest of the same partition day. It never
   reads observations or raw bytes.

2. **Only exactly covered hours count; a gap is never a zero.** An hour counts
   when both of these hold:
   - its metric row is `qualified`;
   - its coverage row says the whole hour was qualified.

   It then contributes 3600 s of exposure and its exact value. Every other hour
   has zero exposure and contributes nothing, not a zero count:
   - a partial hour (its value is only a lower bound);
   - an unknown or failed hour;
   - an hour with no manifest or row;
   - an hour whose coverage row disagrees.

3. **Coverage-weighted baseline** (`backend/eye/worker/baselines.py`, pure).
   - **Bins:** a target bin is `bin_hours` long, aligned to the UTC day.
   - **History:** the same bin on each of the previous `history_days` days.
   - **Rate:** total exact count over total exact exposure, so a bin covered for
     five of six hours weighs five sixths of a bin.
   - **Expected:** that rate times the target's own exposure.

4. **Abstention, with one of two reason codes.**
   - `target_not_covered`: the target's exposure is below
     `min_target_coverage_pct` of the bin.
   - `history_insufficient`: the history holds less exposure than
     `min_history_bins` whole bins.

   An abstained result has no observed, expected or score value. The database
   refuses one that has (`backtest_gap_is_not_zero`), and refuses an evaluated
   result without exposure (`backtest_evaluated_has_exposure`).

5. **Detection is one-sided and exact.**
   - **Variance:** the Poisson variance of the target plus that of the
     estimated rate, scaled by the history's over-dispersion (Pearson, at least
     1). The expected value is floored at 1, so an empty history cannot make any
     count look extreme.
   - **Detected:** the excess over expected is at least `min_excess` and at
     least `threshold_sigma` standard deviations.
   - **Exact:** both comparisons use fractions, so a replay cannot round
     differently. The stored score is rounded to six places for reading only.
   - **Defaults:** `[research]` in the configuration: hourly bins, 14 history
     days, 3 history bins, 5 sigma, an excess of 4, the whole target bin
     covered.

6. **Facts, never prose.** A result is a verdict (`detected`, `not_detected`,
   `abstained`), a reason code from the fixed list, and numbers. Its `detail`
   object holds integers only; the database refuses any other value
   (`backtest_detail_is_numeric`). The commands print JSON. Nothing writes a
   sentence about a result.

7. **A run cites its manifests and is recorded append-only** (migration 0008).
   - **Check first:** before computing, `db-backtest` re-derives every manifest
     it is about to cite (`rollups.check`). If any is not `valid` now, for
     example after a late arrival or correction that has not been rolled up,
     the run is refused and nothing is recorded.
   - **Check and record in one locked transaction (O70):** the transaction
     first takes SHARE locks on the batch, evidence, transit-run and manifest
     tables. A capture, a settling batch, a transit run or a new manifest then
     waits until the run is committed, so nothing can make the checked
     manifests stale between the check and the commit. Other readers, and
     other backtests, are not blocked. `backtest.compute` is a read-only
     preview; `backtest.store` re-derives the run under the lock and refuses
     it if anything changed since it was computed.
   - **Database guard (O70):** independently of the Python code, a trigger on
     `eye.backtest_run` takes the same SHARE locks on `eye.capture_batch`,
     `eye.derivation_run` and `eye.derivation_manifest` (held until commit)
     and refuses a run that cites a manifest that is not current:
     - a later manifest exists for its key;
     - a settled batch of its source and layer overlaps its day and is not
       included;
     - for a transit-daily manifest, a newer transit run exists for the same
       source, line, line version and algorithm (rollups read the newest run,
       even one derived from the same captures).
   - **What the guard covers, and what it does not:** those are every database
     input of a rollup. A day's observations and coverage belong to the
     settled batches whose request overlaps it (capture rejects records outside
     the request) and are append-only, and so are a transit run's counts.
     Inputs held in code or configuration (derivation versions, count line
     definitions, the transit algorithm version) are not database state, so
     only the application's full re-derivation (`rollups.check`) covers them.
     A test checks that the only code that records a run does so inside the
     locked transaction, after that re-derivation.
   - **The guard requires READ COMMITTED:** in that mode each statement in the
     trigger takes its snapshot after the lock is granted, so it sees a
     correction that committed while it waited. A lock cannot refresh a
     REPEATABLE READ or SERIALIZABLE snapshot, so the trigger refuses to record
     a run in such a transaction at all. The application records in READ
     COMMITTED.
   - **Recorded:** `eye.backtest_run` holds the series, the range, the
     parameters, the cited manifest ids, an input checksum (manifest ids and
     their output checksums), the result counts and an output checksum.
     `eye.backtest_result` holds one row per target bin. Both are append-only,
     and a run may cite only existing manifests.
   - **Id:** a run's id is derived from the algorithm version, series, range,
     parameters and input checksum. Running again on the same manifests is a
     no-op.

8. **Late corrections change a result only through a new derivation.** A late
   arrival makes the cited manifests stale, which refuses a new run. Then:
   - `db-rollup` records new manifests and keeps the old ones (ADR 0007).
   - The next `db-backtest` records a new run with a new id over the new
     manifests.
   - The old run and its results stay unchanged and still replay.

9. **Replay re-reads the cited manifests, never the current ones.**
   `db-backtest-replay --run ID` recomputes from the manifests the run cites. It
   compares:
   - the input checksum;
   - the run id;
   - the output checksum, both of the recomputed results and of the stored
     rows;
   - every result row.

   Any difference fails the replay (exit 5), and a replay that cannot run is
   reported UNVERIFIED. Because manifest ids are content-derived, a separate
   database built from the same fixtures reproduces the same run id and
   results.

10. **Bounds: refused, never cut short.**
    - **Range:** at most `max_days` target days (default 92).
    - **Output:** at most `max_output_rows` results (default 10,000).
    - **History:** at most `history_days` (at most 60) before the range.
    - **Memory:** input is therefore at most (92 + 60) × 24 hours of two
      rollups.
    - **Database:** the tables repeat these limits (a range under 366 days,
      at most 100,000 rows, at most 10,000 cited manifests).

## Controls (each beside a successful control)

Fixture: [`tests/fixtures/synthetic/research/series.v1.json`](../../tests/fixtures/synthetic/research/series.v1.json).
It holds 22 invented days of hourly vessel-state counts (Poisson around a
daily pattern, seed 1) in four scenarios, loaded through the real capture
pipeline and rollups in three-hour captures:
- **null:** the base counts;
- **planted:** 20 extra states at 2026-03-22 13:00;
- **shuffled:** the null counts permuted across all hours and days (seed 2);
- **outage:** hours 06 to 08 failed on every day but the last.

Every scenario also has a provider timeout (2026-03-22 18:00 to 21:00) and a
partial capture (2026-03-18 09:00 to 12:00). A test regenerates the fixture
from its seeds exactly. Backtests cover 2026-03-15 to 2026-03-22 (192 hourly
bins).

| Control | Negative | Positive |
|---|---|---|
| Abstention | Unknown, failed, partial or missing target hours abstain (`target_not_covered`) with no value. A history with two exact bins abstains (`history_insufficient`). In the outage scenario, 30 bins abstain and none is detected. A **zero-filling mutant** reading the same manifests detects ordinary morning traffic. The database refuses an abstained row with a zero, an evaluated row without exposure, and text in `detail`. | The same hour covered with nothing seen is a real 0 (`not_detected`). Three exact history bins suffice. A six-hour bin with three unknown hours is judged on its three covered hours (expected 6, not 12) when 50% coverage is allowed. Hour 09 of the outage day is evaluated. A well-formed row is accepted. |
| Planted signal | The same bin in the null scenario is `not_detected` (observed 1). | The planted bin, and only it, is `detected` (observed 21, score 11.1). |
| Shuffled and null | Null and shuffled scenarios: 0 detections. | Not vacuous: 186 of 192 bins evaluated; only the six uncovered bins abstain. Over 60 further seeded series, false detections are 3 of 11,520 null bins and 4 of 11,520 shuffled bins (test bound: under 0.2%), and a planted +20 is found in 60 of 60. |
| Late corrections | After a late backfill, a new run is refused (stale manifests) and nothing is recorded. A **mutant** that reads the current rollups instead of the cited manifests changes the old run's result. | The old run still replays identically. After `db-rollup`, a new run with a new id records the new result: exactly hours 18 to 20 of 2026-03-22 change, from abstained to evaluated. |
| Replay | With the append-only trigger disabled in a disposable database, an altered stored result fails replay and names the row. An update with the trigger on is refused. An unknown run id fails. | A replay is identical. Running again records nothing new. A separate database built from the same fixtures gives the same run id, manifests and stored results. Reversing the series' order changes nothing. |
| Check-to-record race (O70) | A late correction ingested while a backtest is between its checks and its commit waits for the lock and times out (`55P03`); it cannot commit in between. A run checked before a correction and recorded after it is refused by `store`, and, with the Python lock and recheck bypassed, by the database (`no longer current`). Nothing is recorded. On `e27ed0b`, the same sequence recorded the stale run. | The concurrent run is recorded and replays identically; the correction then commits. A run whose inputs did not change between a separate check and record is recorded and replays, and the database accepts it. |
| Database guard alone (O70 follow-up) | A REPEATABLE READ snapshot taken before a late correction commits cannot record the checked run (`READ COMMITTED` required). A READ COMMITTED insert of that run waits in the guard for the correction's open transaction, then refuses the run (`no longer current`). Nothing is recorded. On `87f954a`, the REPEATABLE READ insert was accepted. | Unchanged inputs inserted directly in READ COMMITTED are accepted and replay. After `db-rollup`, the `db-backtest` command records a run and `db-backtest-replay` reports it identical. |
| Newer transit run (O70 follow-up) | A newer transit run from the same captures, with no new batch or manifest, makes the cited transit-daily manifest `stale` (`rollups.check`). A direct READ COMMITTED insert of the earlier preview is refused (`no longer current`), and `store` refuses it too. Nothing is recorded. On `52f8f12`, the direct insert was accepted. | Unchanged transit inputs inserted directly are accepted and replay. After a new rollup, the manifest is `valid` again and a new run is recorded and replays. The only code that records a run does it inside `locked()` (`store`, `run_backtest`). |

**Old code comparison.** Main (`7cfb9792`) has no baseline, backtest or
migration 0008. The new tests run against main's backend fail at import
(`eye.worker.backtest` does not exist), and that is not evidence of behaviour.
The mutants above show each control would fail if its rule were removed:
- zero-filling gaps breaks abstention;
- unpinned replay breaks late-correction safety;
- an altered stored row breaks replay;
- the same detector without the plant finds nothing.

## Review findings (2026-10-08)

- **O70, check-to-record race:** on `e27ed0b` the backtester checked its
  manifests in a read-only transaction, ended it, and recorded the run in a
  separate transaction. A late correction committing in between let a run be
  recorded after its manifests went stale (reproduced: the run was recorded
  while its 2026-03-22 coverage and observation manifests checked `stale`).
  Checking and recording now share one transaction that holds SHARE locks
  from before the check until commit, and the database refuses a run citing a
  manifest that is not current. Migration 0008 is revised in place: it has
  never been merged or applied outside a disposable test database.
- **O70 follow-up, database guard under an older snapshot:** on `87f954a` the
  trigger's lock could not refresh a REPEATABLE READ snapshot. A direct insert
  in a transaction whose snapshot predated a late correction recorded the
  stale run (reproduced). The trigger now refuses any transaction that is not
  READ COMMITTED; in READ COMMITTED it sees every correction committed before
  its lock was granted (tested with the correction committing during the
  wait). The database guarantee is therefore: a run is recorded only in READ
  COMMITTED, and only if its manifests are current at commit.
- **O70 follow-up, newer transit run:** on `52f8f12` the trigger checked only
  batches and manifests. A newer transit run from the same captures made a
  cited transit-daily manifest stale without adding either, and a direct
  insert recorded the stale run (reproduced). The trigger now also locks
  `eye.derivation_run` and refuses a transit-daily manifest whose transit run
  has a newer run for the same source, line, line version and algorithm.

- **Unsupported series (after merge, migration 0009):** `Series.parse`
  accepted a transit series for a position source that never produces transit
  rollups, an unconfigured count line, and a layer the source does not
  provide. Each read no manifest of its own series, so every hour abstained
  and an empty run was recorded (reproduced on `b1f3064`; one such run cited
  no manifest at all). Now:
  - `Series.parse` refuses a layer the source does not provide;
  - every backtest first requires the series to be one that rollups derive
    for the configured count lines (`rollups.required`);
  - the database (migration 0009) refuses a run that cites a manifest of
    another series, or no manifest of its own series.

  A valid transit series and a valid observation series still record and
  replay beside the refusals.

## Consequences and open decisions

- **Statistics are a starting point, not a forecast.** The detector flags
  hours that are unusually busy against their own recent history. It says
  nothing about cause, and a detection is not a prediction. Its false
  detection rate was measured only on invented Poisson series; real feeds will
  need their own measured rates before any result is shown to a user.
- **Partial hours are never used.** A partial hour's value is a lower bound,
  so the baseline ignores it even when that bound alone would already exceed
  the threshold. Using lower bounds for one-sided detection is a possible later
  refinement.
- **Only upward detection.** A quiet period is not detected. Detecting drops
  needs a separate rule, because a drop is easily confused with a coverage gap.
- **Not backed up.** Backtest runs are not part of the synthetic backup
  generation. They can be recomputed from the manifests that are backed up.
- **Locks:** recording a run briefly holds SHARE locks on the batch,
  evidence, transit-run and manifest tables, so captures, transit runs and
  rollups wait for it. A backtest is bounded (decision 10), so the wait is short; a writer
  with a lock timeout fails rather than waiting, and its batch is retried
  like any refused capture.
- **Not served.** Showing results in DESK would need a new wire version.
- **Real-data deletion still blocked.** It needs database role separation and
  a proven real backup and restore in the private environment (Package 8).
