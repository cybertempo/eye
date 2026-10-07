# Migrations

Forward-only SQL files named `NNNN_lower_snake_name.sql`, applied in order by
`backend/eye/storage/migrate.py` (`python -m eye db-migrate`).

## Guarantees

- Each file runs in one transaction with its version row, under an advisory
  lock. A failure rolls the whole file back; prior schema and data are untouched
  (proved by `tests/test_storage.py::test_failed_migration_leaves_prior_data_intact_and_good_one_applies`).
- Applied files are checksummed in `public.eye_schema_migrations`; editing one
  after it ran is refused. Versions must run 1..n without gaps.
- A migration cannot commit partial work. Top-level transaction control
  (`BEGIN`, `START`, `COMMIT`, `END`, `ROLLBACK`, `ABORT`, `SAVEPOINT`, `RELEASE`,
  `PREPARE`) is refused anywhere, including after another statement on the same
  line; quotes, comments and dollar-quoted PL/pgSQL bodies are understood.
  Each statement is sent unaltered in its own Parse message, which PostgreSQL
  refuses if it holds two commands, and PostgreSQL refuses `COMMIT` inside
  `DO`/`CALL` here. The transaction id is checked after every statement.
- Raw evidence, observations and coverage are append-only (triggers). Since
  0007 the one exception is pruning a raw evidence row's **bytes** through the
  checked retention transition: the database refuses it unless a `pruned`
  decision in the same transaction names the batch, the partition's lateness
  window (at least 48 hours) has closed by the database clock, every required
  current manifest is cited, complete and checked valid in that transaction,
  and a verified synthetic backup holds those exact bytes and manifests
  (`eye.pruning_refusal`). No row is ever deleted.

## Applied migrations

| Version | Adds |
|---|---|
| 0001 | capture batches, raw evidence, observations, receipts, coverage, version view |
| 0002 | count lines, derivation-run log with input batches, current vessel tracks, track gaps, line crossings and transit counts, immutable per-run crossings, gaps and counts |
| 0003 | feed epoch and append-only feed change log (one row per settled capture batch and per derivation run), written by triggers in commit order, for API cursors and deltas |
| 0004 | record identity includes the layer: version history ranks within (source, layer, record, observed time); refuses a database that already holds observations, whose ids came from the earlier rule (rebuild it from raw evidence) |
| 0005 | append-only event-claim ledger: claims (one per case version, content-derived id), receipts, and a version view ranked by publication time with conflicts left unresolved; constraints refuse motion inference as anything but a review candidate and a sourced report without evidence; new objects only, no existing row read or rewritten |
| 0006 | append-only news and media evidence, separate from event claims: media items (one per item version, content-derived id; metadata and a link only), receipts, and a version view ranked by the source's revision time with conflicts left unresolved; constraints bound untrusted text, require a licence and attribution exactly when an item is licensed, and keep a place's role, method and precision together; new objects only, no existing row read or rewritten |
| 0007 | derivation manifests (append-only; input batches, checksums, watermark, versions, outputs, coverage) with rollup rows that refuse a zero for unknown coverage and any coordinate; manifest validations; synthetic backup proofs; retention decisions; raw evidence may lose its bytes only through a `pruned` decision in the same transaction after the lateness window, citing current, complete, freshly validated manifests (including `ledger-replay`) and a verified backup (`eye.pruning_refusal`; the trigger `raw_evidence_append_only` keeps its name, its function is replaced); adds a nullable column and replaces one CHECK on `eye.raw_evidence`, otherwise new objects only |

Merged migrations are never edited; a change is a new file.

## Review checklist for a new migration

- Transactional DDL only (no `CREATE INDEX CONCURRENTLY`, no `VACUUM`).
- No data rewrite of evidence, observations or coverage.
- A test that applies it over loaded fixtures and shows prior data unchanged.
- Recorded in the PR with its expected lock impact.

## Restore

There is no down-migration. On the **private server**, take a verified backup
before applying a release's migrations; if a migration must be undone, restore
that backup and redeploy the previous release. Package 5 adds a **synthetic**
backup and restore drill (`scripts/restore-drill.sh`, developer laptop or CI):
it proves the code path on invented data, not the private backup, which is
still a documented requirement proved on the server (Package 8).

## Commands

| Where it runs | Command |
|---|---|
| Developer laptop, CI | `scripts/verify.sh` (applies migrations to disposable databases) |
| Developer laptop | `EYE_DATABASE_URL=postgresql://… PYTHONPATH=backend .venv/bin/python -m eye db-migrate --config config/eye.example.toml` |
