# ADR 0002: Package 1 contracts and storage

Date: 2026-09-28. Status: proposed (awaiting owner review).

## Decisions

1. **Wire schema as JSON Schema (draft 2020-12, restricted subset).** One file,
   `schemas/eye-wire.v1.schema.json`, covers REST and WebSocket messages. EYE
   ships two small interpreters of the subset (Python and TypeScript) instead of
   a runtime library, so the demo server stays standard-library only. They are
   cross-checked against the reference `jsonschema` library (test-only).
2. **Generated client types** from the same file by `scripts/gen_wire_types.py`,
   committed and checked for staleness in `verify`.
3. **Outage semantics in the contract and the database.** Coverage `unknown` or
   `failed` requires a NULL metric and a reason, enforced by the wire schema
   (`CoverageMissing`) and by the `coverage_missing_is_null` CHECK constraint.
4. **PostgreSQL 17 + PostGIS 3.5**, image `postgis/postgis:17-3.5` pinned by
   digest. No TimescaleDB feature is used; its edition and licence decision is
   deferred (brief section 3).
5. **pg8000** (BSD-3-Clause, pure Python) as the driver, chosen over psycopg
   (LGPL-3.0) because the project licence is undecided and a permissive,
   binary-free driver keeps that decision open.
6. **Own forward-only migration runner** instead of Alembic or similar:
   transactional per file, checksummed, advisory-locked, no ORM. A migration
   cannot end the runner's transaction: a SQL-aware splitter refuses top-level
   transaction control anywhere on a line; each statement goes unaltered in its
   own extended-protocol Parse message, which PostgreSQL refuses if it holds
   two commands; PostgreSQL refuses COMMIT inside DO/CALL in a transaction; and
   the transaction id is checked after every statement.
7. **Deterministic ids.** UUIDv5 over content: batch = source + evidence SHA-256;
   observation = source + record id + observed time + content hash (content
   includes the source publication time; EYE receipt time is excluded, so a
   duplicate delivery of the same version collapses); coverage = batch + layer +
   metric. Rebuild and replay reproduce every id in any load order.
8. **Archive, then commit.** Raw bytes and a `pending` batch are stored first;
   parsing reads the stored bytes back and verifies the checksum. A crash leaves
   a discoverable pending batch that `db-replay` completes.
9. **Corrections append; meaning is derived.** A later version of the same
   source record and observed time is a new row. `eye.observation_version`
   orders versions by source publication time (then id) and derives version,
   supersedes link and current flag, so load order cannot change them. Two
   versions with the same publication time are flagged `publication_conflict`.
10. **Time provenance.** Three separate facts: `observed_time` (source event
    time, from the provider record), `source_published_time` (when the source
    issued that version, from the provider record) and `received_time` (EYE
    receipt, stamped by EYE's capture adapter as the attempt's `finished_at` and
    stored per delivery in `eye.observation_receipt`). A provider record that
    carries its own receipt time is rejected. Enforced chronology: observed <=
    published <= received = attempt finished >= requested interval end, in the
    parser and in database constraints and triggers.
11. **Nothing usable is not zero.** A provider `ok` response whose records are
    all rejected is `failed` with a NULL metric; only an `ok` response with no
    records is a measured `qualified` zero.
12. **Replay verification compares values.** `db-replay` re-derives every batch
    outcome, observation value, receipt, coverage row and version link from the
    stored evidence with the same derivation used to commit, and reports any
    missing, extra or different row, plus evidence that fails its checksum.
13. **Migration 0001 amended before release.** PR #2 changed 0001 in place
    (receipts table, version view, confidence as double precision,
    `eye.iso_utc`). It had not been merged or applied outside disposable test
    databases; after merge, changes need a new migration.

## Consequences

- `scripts/verify.sh` now needs Docker (or `EYE_TEST_DATABASE_URL`); without a
  database it fails rather than skipping.
- The demo still serves the static snapshot file; serving from the database is
  Package 3. Event claims (the demo's road events) are not stored yet (4c).
- The PostGIS image is published for amd64 only; Apple Silicon laptops run it
  under emulation or use a local PostGIS through `EYE_TEST_DATABASE_URL`.
