# ADR 0007: Package 5 lifecycle slice (rollups, manifests, retention, synthetic drill)

Date: 2026-10-07. Status: proposed (awaiting owner review). Revised the same day for
audit findings O67 to O69 (see "Review findings" below).

Scope:
- **Built:** replayable rollups, derivation manifests, one checked retention coordinator
  (deletion off by default) and a synthetic backup, verification and restore drill.
- **Not built:** research baselines and the backtester (the rest of brief §7, Package 5).
  There are no wire or browser changes: rollups and manifests are reported by command
  only, and the wire schema stays `eye.wire/4`.
- **Data:** invented fixtures only. No provider, account, credential, private host or
  paid request. Migrations 0001 to 0006 are unchanged; everything new is migration 0007.

## Decisions

1. **A partition is one source, one layer and one UTC day.** A capture batch belongs
   to the partition of its requested start. Its rollups are computed for every day
   its request overlaps, and retention needs the manifests of all of those days.

2. **Four rollups, each a pure function of stored records** (`backend/eye/worker/rollups.py`).
   None of them reads raw bytes, so they survive pruning and a restore reproduces them.
   - **Coverage is judged over the footprint.** The footprint is every area the
     partition's captures requested that day (requested areas are rectangles). At each
     instant, each point of the footprint takes the best state of the captures whose
     area contains it, or *uncaptured*; the instant takes the weakest of those. A
     qualified capture of the west therefore never vouches for a simultaneous failed
     capture of the east, and an area requested only part of the day is uncaptured for
     the rest of it. A cell uses the cell itself as its footprint.
   - **`coverage-hourly/2`:** for each hour and the day, the seconds of qualified,
     partial, unknown, failed and uncaptured capture. This is an exact account of EYE's
     own capture log, so its rows are qualified. Its value is the usable (qualified or
     partial) seconds.
   - **`observations-hourly/2`** (position sources): distinct observed states, meaning
     one per record and observed time however many versions it has.
     - *Unknown:* an hour with any second lacking usable coverage is unknown with no
       value, never 0.
     - *Lower bound:* partial coverage makes the hour partial.
     - *Exact:* only a fully qualified hour is exact, so a covered hour with nothing
       observed is a genuine 0.
     - *Day:* the day row is unknown if any hour is.
   - **`cells-daily/2`** (position sources): current observed states per 0.1° cell.
     - A cell is named by its index and bounds. A database constraint
       (`rollup_has_no_position`) refuses a `lon`, `lat`, `position`, `centroid` or mean
       key, so no row can be read as a vessel's or aircraft's position.
     - Only occupied cells have rows. A missing cell is not a measured zero.
     - States whose latest versions conflict have no chosen position. They are counted
       in an `unplaced` row and credit no cell.
   - **`transit-daily/1`** (`synthetic-ais` vessels, per count line): the hourly counts
     of the latest recorded transit run, copied with their state and reason. Any
     unknown hour makes the day unknown; any partial hour makes it a lower bound.
     - The rollup refuses to use a run that misses one of the day's committed batches
       (`RollupStale`).
     - `db-rollup` re-runs the transit counter first when its run is out of date.
   - **`ledger-replay/1`** (every source): this one does read raw bytes. It re-derives
     every batch that starts on the day from its stored bytes and compares the result
     with what that batch stored: the batch outcome, observations, event claims, media
     items, their receipts, coverage and capture facts. Any difference refuses it, so a
     corrupted derived row has no valid manifest. Its rows count each kind of row with a
     checksum of the stored rows. Version links are checked by `db-replay` instead,
     since they depend on every batch of a record. Once a partition's bytes are pruned
     this check cannot run: it is UNVERIFIED, never valid, and the manifest recorded
     before pruning stays current. Only the restore drill can replay it.

3. **Manifests are append-only and identify their inputs** (`eye.derivation_manifest`,
   migration 0007).
   - **Contents:** each manifest records:
     - the partition day and input interval;
     - the derivation and its version, plus the required upstream versions
       (`transit-counter/2`, `coverage/2`);
     - the input batch ids and count, input row count and input checksum (for
       `ledger-replay`, the batch ids and their evidence checksums);
     - the watermark (latest archive time of its inputs) and the lateness deadline;
     - for transits, the transit run it used;
     - the output row count, output checksum and a coverage summary.
   - **Id:** the id is derived from the partition, derivation, version, scope and input
     checksum. The watermark is excluded, so a restore reproduces the id.
     Re-running on unchanged evidence is a no-op.
   - **Current:** `eye.current_manifest` is the latest manifest per key, and
     `eye.current_rollup` is its rows.
   - **Validation state:** recorded in `eye.manifest_validation` as `valid`, `stale`,
     `failed` or `unverified`.
   - **Backup proof:** the proof a pruning relied on is in `eye.manifest_retention_proof`.

4. **Validation re-derives and compares.** A check recomputes the derivation from
   current evidence.
   - **Stale:** a different input checksum (a late arrival or a correction) or an old
     derivation version.
   - **Failed:** different outputs, stored rows that do not match the manifest, a
     refused derivation, or a transit replay discrepancy (`transits.verify` and
     `audit_run`).
   - **Unverified:** a check that cannot run. It is never valid.
   - **Late arrivals:** a late arrival or correction yields a new manifest when the
     worker runs again. The old manifest and its rows stay as history, and the metric
     that is no longer current is reported stale rather than kept silently.

5. **Retention prunes raw evidence bytes only** (`backend/eye/worker/retention.py`).
   - **Kept rows:** the `eye.raw_evidence` row keeps its id, checksum, size and media
     type, and records the decision that pruned it.
   - **No deletion path:** observations, receipts, event claims, media items, coverage,
     derivation runs, manifests and decisions keep their append-only triggers. Nothing
     deletes from them.
   - **Eligibility requires all of these:**
     - the lateness window is closed (`retention.lateness_hours`, default and minimum
       48);
     - no batch is pending, and every batch's stored bytes match their checksum;
     - every required manifest of every touched day validates now, and the
       partition's own `ledger-replay` validates (its bytes reproduce every ledger row
       they produced);
     - the most recent backup verification did not fail;
     - the latest verified synthetic proof holds every batch's exact bytes and every
       current manifest;
     - that copy, re-read from the backup directory, is present and intact.
   - **Unverified blocks too:** a check that cannot run (database error, no or
     unreachable backup directory) is UNVERIFIED and blocks the partition.

6. **One coordinator: plan reads, execute locks and rechecks.**
   - **`db-retention-plan`:** read-only. It records nothing.
   - **`db-retention-execute --partition SOURCE:LAYER:DAY`** is refused unless all hold:
     - `retention.allow_deletion = true` (default false, and refused by the
       configuration loader outside demo mode);
     - demo mode;
     - an approved synthetic source.
   - **Lock and recheck:** execute takes a `SHARE ROW EXCLUSIVE` lock on the batch,
     evidence, derivation-run, manifest and backup-proof tables. It then re-runs every
     check inside that lock, records the checks and the decision (refusals included),
     and prunes only on a passing recheck. A capture archived or committed concurrently
     waits for the decision or is seen by the recheck, so it cannot race it.
   - **Never automatic:** setup, verify, the demo, the container and CI never run
     execute. Tests run it only on disposable databases, on a partition they name.

7. **The database guards pruning independently.** Migration 0007 replaces the
   function behind `raw_evidence_append_only` (the trigger keeps its name).
   - **Refused:** DELETE, and every UPDATE except setting `content` to NULL with
     `pruned_by_decision`.
   - **Required facts** (`eye.pruning_refusal` names the first that fails):
     - a `pruned` decision written in the same transaction that names the batch and
       its partition, which holds no pending batch;
     - the partition's lateness window has closed by the database clock: day end plus
       the decision's `lateness_hours`, which cannot be below 48;
     - the batch's ledger rows (outcome, observations, event claims, media items,
       receipts, coverage) still match the digest the database sealed when the batch
       settled (`eye.batch_seal`). Only the settling trigger writes a seal; a direct
       insert is refused and seals are append-only. Once a batch settles its ledger is
       closed: no receipt or coverage row may be added for it, so a later row cannot
       change what was sealed, in a separate statement or in the pruning statement
       itself;
     - a verified synthetic proof listing the batch with these exact bytes and every
       cited manifest, with no later failed verification;
     - for every day the batch's request overlaps, a current coverage manifest (and
       observation and cell manifests for position captures, and for `synthetic-ais`
       vessels a transit manifest per registered count line, at its latest version),
       and for its own day a current `ledger-replay` manifest; every current manifest
       of those days cited;
     - every cited manifest current, including every settled batch it should (so a
       late arrival leaves it incomplete), and checked `valid`, and never otherwise, in
       the same transaction (`eye.manifest_validation.txid`).
   - **What it cannot do:** it does not parse evidence bytes. A `valid` check recorded
     by a direct writer no longer suffices once a ledger row changes after its batch
     settled, because the seal no longer matches. Rows that were already wrong when
     the batch settled (an ingest defect, or a batch settled by direct SQL) match
     their seal; only the coordinator's `ledger-replay` catches those. A role with
     table-owner rights can disable any trigger, the guard included; keeping the
     application role from owning the schema belongs to the private installation.
   - **Tested alone:** tests bypass the Python checks and show the database refuses on
     its own: an early partition, an unmanifested one, a late arrival, an unchecked
     manifest, a corrupted ledger falsely checked `valid`, an AIS partition without
     its transit manifest, a forged or rewritten seal, an extra coverage row added in
     the pruning statement's own data-changing CTE (refused by the closed ledger, and
     with that trigger off still by the seal), a decision from another transaction
     and a failed proof. With the trigger
     also disabled, the same negative control fails, which shows it tests the guard.

8. **Synthetic backup target only** (`backend/eye/worker/backup.py`).
   - **Store:** `synthetic-directory` is a disposable local directory of
     content-addressed evidence and line copies plus canonical generation manifests.
     A generation's id is the SHA-256 of its bytes.
   - **Verification:** an independent read-back. It re-hashes every file and compares
     each checksum with the database's own record, then records `eye.backup_proof` as
     `verified` or `failed`. The table refuses any target class but
     `synthetic-directory` and any proof not marked synthetic.
   - **Restore drill:** restores a generation into an empty database through the
     ordinary archive-and-commit path. It then:
     - re-derives transits with the backed-up intervals;
     - recomputes the listed rollups;
     - runs replay verification;
     - compares every named metric (hourly transit counts, every rollup row) and every
       manifest id.

     Any missing or damaged copy, refused batch, discrepancy or differing metric makes
     the report `failed`. Only a complete, exact reproduction is `verified`.
   - **What it does not prove:** every report and command output carries the label
     *synthetic code test only*. It does not prove an off-site Google Drive backup,
     restic, the private installation, or any recovery point or restore-time objective.
     Those are Package 8 and are proved on the owner's server.

9. **Replay after pruning.** `db-replay` cannot re-derive rows from bytes that were
   pruned.
   - **Pruned rows:** a pruned batch, and rows whose every receipt is from pruned
     batches, are taken as stored. Only the restore drill re-derives them, from the
     backup that the pruning decision cites.
   - **Version history:** still checked across all rows.
   - **Missing bytes:** bytes missing without such a decision are reported as a
     discrepancy.

## Controls (each beside a successful control)

| Negative control | Positive control |
|---|---|
| A failed rollup (cell limit refused) or a missing manifest blocks retention | An eligible synthetic partition passes |
| No backup, a corrupt or missing copy, a failed latest verification, or an unconfigured or unreachable directory blocks (or is UNVERIFIED), and the bytes stay | A verified backup passes and the partition is pruned |
| A late arrival or correction makes the old manifest stale and blocks retention. History stays, and the new manifest carries the new value | An unchanged partition validates |
| A failed or uncaptured hour is unknown with no value, and the database refuses a zero for it | A covered hour with nothing seen is a genuine 0 |
| Qualified west and failed east at the same time: 0 qualified and 3600 failed seconds, observations unknown | The same area fully qualified (one capture, or west and east both qualified): 3600 qualified seconds, an exact count |
| A corrupted event claim, media item or observation fails `ledger-replay` and blocks pruning; bypassing that check would let it pass | Intact event, news and position ledgers are eligible |
| A damaged, missing, truncated or altered generation, or a non-empty target, is never `verified` | A clean restore reproduces 404 named metrics and 25 manifests (demo fixtures) |
| Execution is refused when disabled, outside demo or for a non-synthetic source. A concurrent late write blocks it, and a late write after planning is caught by the recheck | An explicitly named disposable partition is pruned |
| A direct DELETE or UPDATE, or a decision from another transaction, is refused by the database | The checked path prunes |
| A direct same-transaction decision and pruning of an early partition, an unmanifested one, one with a late arrival, or one whose manifests were not checked valid, is refused by the database; a lateness below 48 hours is refused | The coordinator prunes an eligible partition; the same direct write succeeds once every recorded fact holds (the known limit above) |
| A direct pruning of a corrupted ledger whose manifests the writer marked `valid` is refused (seal mismatch); a direct seal insert or rewrite is refused | The coordinator prunes an intact partition of the same database |
| A direct AIS pruning without a transit manifest per count line is refused; the coordinator also blocks it, and blocks AIS with no count line configured | With the transit manifests recorded and backed up, the coordinator prunes the AIS partition |

**No old-code comparison.** Main has no retention, manifest or backup path, so running
these tests against it would show only import errors. That is not evidence. Instead:
- *Guard bypass:* tests bypass a guard in a disposable database (the Python checks, the
  lateness check, then also the database trigger) and show the corresponding negative
  control would then fail.
- *Old migrations still apply:* the earlier migration tests now apply exactly their own
  version, and a new test shows 0007 leaves every earlier table's rows unchanged.

## Review findings (2026-10-07)

- **O67, coverage overclaim:** coverage took the best state at each instant whatever
  its area, so a qualified west hid a simultaneous failed east. Coverage is now judged
  over the footprint (decision 2), and the coverage, observation and cell rollups moved
  to version 2.
- **O68, incomplete retention gate:** event and news partitions needed only a coverage
  manifest, so their claim and media rows were never checked against the bytes before
  pruning. Every partition now needs a valid `ledger-replay` (decision 2).
- **O69, database guard bypass:** the trigger accepted any same-transaction `pruned`
  decision with a verified proof, so a direct writer could skip the lateness and
  manifest checks. The guard now checks them itself (decision 7).

- **O69, second round:** a same-transaction `valid` check needed no re-derivation, and
  an absent AIS transit manifest was not required. The database now seals each batch's
  ledger rows when it settles and refuses pruning when they no longer match, and it
  requires a transit manifest per count line for `synthetic-ais` vessels.

- **O69, third check:** a coverage row added in a data-changing CTE of the pruning
  statement was refused by the seal on 2282bbe (the guard's digest sees it). A lone
  coverage or receipt row added after settling was accepted, though, which changed the
  sealed ledger. Settled batches now have a closed ledger.

These revise migration 0007 in place: it has never been merged or applied outside a
disposable test database.

## Consequences and open decisions

- **Baselines and backtester:** coverage-weighted baselines and the backtester (with
  planted-signal and shuffled/null controls) remain to be built in Package 5.
- **Private backup target:** the private target (restic export, Drive, an enrolled SSD,
  with its own proof table rows) needs its own decision and a target class beyond
  `synthetic-directory`. Until then, production refuses `allow_deletion`.
- **Provider removal periods:** deletion or refresh obligations for any provider (for
  example a video or photo service) are not encoded. They stay UNVERIFIED until their
  official terms are reviewed, and they would need a separate, narrow path. This slice
  prunes evidence bytes only after backup and never removes ledger rows.
- **API exposure:** rollups are not served over the API. Showing them in DESK would
  need a new wire version.
