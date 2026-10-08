# ADR 0009: The database records whether it is the demo or a private installation

Date: 2026-10-08. Status: proposed (awaiting owner review).

## Context

Audit finding (2026-10-08):
- **Startup:** demo startup checked only that the database was reachable on loopback and migrated.
- **Auth:** demo authentication trusts the configured mode.
- **Commands:** `db-prepare-demo` and the other demo-mode database commands checked only the mode
  flag and a loopback connection.

A private installation can listen on loopback on the same host. A demo configuration pointed at it
would have served its data through fake authentication and written synthetic batches into it.

Reproduced on `b1f3064` (cloud dev container, disposable PostGIS):
- **Private database:** a database migrated in production mode accepted `db-prepare-demo` (23
  synthetic batches written) and demo `serve`.
- **Real data:** a database holding a capture batch of a non-synthetic source also accepted both.

## Decision

1. **The database records its kind once** (migration 0009, `eye.database_identity`).
   - **Shape:** at most one row, with kind `synthetic-demo` or `private`. It is append-only, so a
     claim cannot be changed or removed.
   - **Who claims it:**
     - `db-migrate` in demo mode and `db-prepare-demo` claim `synthetic-demo`;
     - `db-migrate` in production mode claims `private`.
   - **No relabelling:** a database claimed for one kind refuses the other, and no command can
     relabel it.
2. **Demo mode refuses a private or real database before it serves or writes.**
   - **Where it is checked:** demo `serve` and every demo-mode database command (migrate, load,
     derive, rollup, backup, retention, backtest, replay) run the check right after connecting,
     before migrating or writing anything.
   - **What is refused:** a database that is either:
     - claimed `private`; or
     - holding a capture batch of a source whose id does not start with `synthetic-`.

     The second rule catches a private database migrated by an older release that never claimed a
     kind.
   - **What is allowed:** an unclaimed database with synthetic data only (or none). The demo claims
     it.
   - **Naming rule this relies on:** every synthetic source id starts with `synthetic-`, and a real
     source must never use that prefix.
3. **Production startup refuses a database that is not claimed `private`.** It refuses one claimed
   by the demo and one claimed by nobody. Production `db-migrate` makes the claim.
4. **A check that cannot run is a refusal** (`UNVERIFIED`, exit 5, or a startup refusal), never a
   pass.

## Controls (each beside a positive control)

`tests/test_database_identity.py`:

| Negative | Positive |
|---|---|
| On a database claimed `private` by production `db-migrate`, every demo writer is refused (exit 2) and nothing changes: migrations, batches, manifests, transit runs, backtest runs and the claim. The writers are `db-migrate`, `db-prepare-demo`, `db-load-fixtures`, `db-derive-transits`, `db-rollup`, `db-retention-execute` and `db-backtest`. Demo `serve` is refused (exit 4). | Production startup accepts that database. |
| An unclaimed, migrated database holding a `real-provider` batch: `db-prepare-demo`, `db-load-fixtures`, demo startup and a demo claim are refused; nothing changes. | A fresh database: `db-prepare-demo` succeeds twice and claims `synthetic-demo`, and demo startup accepts it. |
| A demo database: production startup and production `db-migrate` are refused; the claim cannot be updated or deleted. | A database claimed `private` passes production startup. |
| An unclaimed database: production startup is refused. | |

## Consequences

- **Existing local demo databases:** the next `db-migrate` or `db-prepare-demo` claims them, if they
  hold synthetic data only.
- **Private installation:** it must run `db-migrate` in production mode once to claim its database
  before the API starts.
- **Limits:**
  - This guards against a mistaken configuration, not a hostile database user. A user who can write
    the table on an unclaimed database can claim it.
  - Role separation is Package 8 work.
- **Migration numbering:** this branch starts from main, while PR #13 also adds a migration 0009.
  Whichever merges second renumbers its migration to 0010 before merging.
