# EYE

EYE is a planned self-hosted geospatial and solar-system observation and research
application. This repository holds its public source. **Status: Package 2
(synthetic AIS vertical slice) on Packages 0 and 1.** It contains the layout,
tooling, CI, a small synthetic demo, the versioned wire schema with validation
on both sides, PostgreSQL/PostGIS storage for captures, evidence, observations
and coverage, and a synthetic vessel adapter with track splitting, crossings of
a versioned synthetic count line and coverage-aware transit counts. All data is
invented. No real data feed, real Golden Gate geometry, globe renderer, live API
or AI feature exists yet.

The design brief is [`docs/public-repo-build-brief.md`](docs/public-repo-build-brief.md);
contributor and agent rules are in [`CLAUDE.md`](CLAUDE.md).

## Licence status

**No licence has been chosen.** The source is publicly visible, but no reuse,
modification or redistribution rights are granted, and this is not an open-source
project. There is no `LICENSE` file on purpose; the owner will decide later.
Outside contributions are not being accepted until then. Third-party notices are
tracked separately in [`NOTICE.md`](NOTICE.md).

## Quick start

Every command below is labelled with where it runs. Nothing here needs an
account, a credential or the owner's server, and nothing opens a browser.

Prerequisites (developer laptop): Git, Python 3.11 or newer (CI uses 3.12),
Node.js 22 with npm, and Docker for the disposable test database and the
container check (or set `EYE_TEST_DATABASE_URL` to a disposable PostGIS).

| Where it runs | Command | What it does |
|---|---|---|
| Developer laptop, CI | `scripts/setup.sh` | Creates `.venv` with hash-locked tools, runs `npm ci`, builds the demo page, pulls the pinned PostGIS image. Downloads from PyPI, npm and Docker Hub only. |
| Developer laptop, CI | `scripts/verify.sh` | Boundary scan, lint, format, generated-type check, typecheck, config check and all tests, including PostGIS tests in a disposable loopback container. No network. Fails (never skips) if no database is available. |
| Developer laptop (Docker), CI | `scripts/verify.sh --container` | Adds an image build and a loopback smoke test. |
| Developer laptop | `scripts/demo.sh` | Serves the synthetic demo on `http://127.0.0.1:8765/`. Open that URL yourself; stop with Ctrl-C. |
| Developer laptop (Docker) | `docker compose -f deploy/dev/compose.yaml up --build` | The same demo in a container, published on host loopback only. Run `scripts/setup.sh` first. |

## Database (synthetic data only)

Run on the developer laptop, against a PostGIS you started yourself on loopback.
Put the URL in the environment variable named by `database.url_env`
(`EYE_DATABASE_URL` in the example); it is never written in a file.

```text
PYTHONPATH=backend .venv/bin/python -m eye db-migrate       --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-load-fixtures --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-derive-transits --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-replay        --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-status        --config config/eye.example.toml
```

Demo mode connects only to a loopback database, and only demo mode loads
synthetic fixtures. `db-derive-transits` counts crossings of every line in
`reference/lines/` per hour; `db-replay` also re-derives and checks them. See
[`migrations/README.md`](migrations/README.md),
[`schemas/README.md`](schemas/README.md) and
[ADR 0003](docs/adr/0003-package-2-synthetic-ais.md).

## Configuration

All settings live in one TOML file; [`config/eye.example.toml`](config/eye.example.toml)
is the demo configuration. Set `EYE_CONFIG` to use another file with `scripts/demo.sh`.
Check a file without starting anything (developer laptop):
`PYTHONPATH=backend .venv/bin/python -m eye check-config --config <file>`.

The loader refuses, before opening a socket:

- a non-loopback `bind_host` in demo mode, except the explicit dev-container
  flag pair described in [ADR 0001](docs/adr/0001-package-0-foundation.md)
  (exit code 2);
- any entry in `providers.enabled` (no provider has an approved
  [source-policy](docs/source-policy-register.md) row);
- unknown keys;
- production mode without the private authentication adapter (exit 2 or 3).
  Demo authentication is never a production fallback. Production serving itself
  is not built yet (exit 4 after the adapter check).

## Layout

```text
backend/eye/api/      HTTP API, auth port, demo server
backend/eye/ingest/   capture pipeline and synthetic AIS adapter
backend/eye/storage/  database connection and migration runner
backend/eye/wire/     wire-schema runtime validator
backend/eye/worker/   tracks, line crossings and transit counts
backend/eye/raster/   optional imagery pipeline (Package 6)
config/               example configuration
deploy/dev/           loopback synthetic demo container
deploy/lab/           generic private-deployment templates (placeholder)
docs/                 brief, source-policy register, dependencies, ADRs
migrations/           forward-only PostgreSQL/PostGIS migrations
reference/lines/      versioned synthetic count lines
requirements/         hash-locked Python tool manifests
schemas/              versioned wire schema (eye.wire/1)
scripts/              setup, verify, demo, boundary check, fixture generators
tests/                tests and synthetic fixtures
web/                  browser client (demo page only)
```

## Limitations

- The demo serves one static synthetic snapshot as tables. THEATRE, DESK, the
  live API and WebSocket server (Package 3), event-claim storage (4c) and every
  real adapter are later packages. The WebSocket messages are defined and
  validated but no WebSocket server exists yet.
- A passing CI run shows the public code builds and its synthetic tests pass. It
  is **not** evidence that EYE is installed or secure on the owner's private
  server; that is proved separately (brief §8).
- Dependency and image pins are listed in [`docs/dependencies.md`](docs/dependencies.md).
