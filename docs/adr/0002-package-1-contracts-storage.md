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
6. **Own forward-only migration runner** (about 100 lines) instead of Alembic or
   similar: transactional per file, checksummed, advisory-locked, no ORM.
7. **Deterministic ids.** UUIDv5 over content: batch = source + evidence SHA-256;
   observation = source + record id + observed time + content hash (receipt
   time excluded, so duplicate deliveries collapse); coverage = batch + layer +
   metric. Rebuild and replay reproduce every id.
8. **Archive, then commit.** Raw bytes and a `pending` batch are stored first;
   parsing reads the stored bytes back and verifies the checksum. A crash leaves
   a discoverable pending batch that `db-replay` completes.
9. **Corrections append.** A later record for the same source record and observed
   time is a new row with `supersedes_observation_id`; nothing is rewritten.

## Consequences

- `scripts/verify.sh` now needs Docker (or `EYE_TEST_DATABASE_URL`); without a
  database it fails rather than skipping.
- The demo still serves the static snapshot file; serving from the database is
  Package 3. Event claims (the demo's road events) are not stored yet (4c).
- The PostGIS image is published for amd64 only; Apple Silicon laptops run it
  under emulation or use a local PostGIS through `EYE_TEST_DATABASE_URL`.
