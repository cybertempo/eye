# EYE

EYE is a planned self-hosted geospatial and solar-system observation and research
application. This repository holds its public source. **Status: Package 3
(browser and API) on Packages 0 to 2.** It contains the layout, tooling, CI, the
versioned wire schema with validation on both sides, PostgreSQL/PostGIS storage
for captures, evidence, observations and coverage, a synthetic vessel adapter
with track splitting, crossings of a versioned synthetic count line and
coverage-aware transit counts, and a database-backed API (bounded REST snapshots
and a WebSocket feed of sequenced deltas) serving a browser client with a
token-free THEATRE globe and a DESK observed-transit panel. All data is
invented. No real data feed, real Golden Gate geometry, basemap, CesiumJS
renderer, private installation or AI feature exists yet.

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
| Developer laptop, CI | `scripts/setup.sh` | Creates `.venv` with hash-locked tools, runs `npm ci`, builds the browser client, fetches the headless browser build pinned by the locked Playwright release, pulls the pinned PostGIS image. Downloads from PyPI, npm, Playwright's browser CDN and Docker Hub only. |
| Developer laptop, CI | `scripts/verify.sh` | Boundary scan, lint, format, generated-type check, typecheck, config check and all tests: PostGIS tests in a disposable loopback container and headless browser tests (no window opens). No network. Fails (never skips) if no database is available. |
| Developer laptop (Docker), CI | `scripts/verify.sh --container` | Adds an image build and a loopback smoke test of the demo and its database. |
| Developer laptop (Docker) | `scripts/demo.sh` | Builds and runs the demo container with a disposable PostGIS database (one-run password, nothing stored) on `http://127.0.0.1:8765/`. Open that URL yourself; Ctrl-C stops and removes it. Run `scripts/setup.sh` first. |
| Developer laptop | `EYE_DATABASE_URL=… scripts/demo.sh --local` | The same demo from `.venv`, against a loopback PostGIS you started yourself; loads the synthetic data into it. |

## Database (synthetic data only)

Run on the developer laptop, against a PostGIS you started yourself on loopback.
Put the URL in the environment variable named by `database.url_env`
(`EYE_DATABASE_URL` in the example); it is never written in a file.

```text
PYTHONPATH=backend .venv/bin/python -m eye db-migrate       --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-load-fixtures --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-derive-transits --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-prepare-demo  --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-replay        --config config/eye.example.toml
PYTHONPATH=backend .venv/bin/python -m eye db-status        --config config/eye.example.toml
```

Demo mode connects only to a loopback database, and only demo mode loads
synthetic fixtures. `db-derive-transits` counts crossings of every line in
`reference/lines/` per hour; `db-prepare-demo` runs migrate, load and derive in
one step; `db-replay` also re-derives and checks them. See
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
  Demo authentication is never a production fallback. With the adapter,
  `serve` also needs a reachable, migrated database (exit 2 or 4); every data
  request and every open WebSocket is authorised through the adapter.
- an `api.ws_max_buffer_bytes` smaller than `server.max_response_bytes`.

## Browser and API (Package 3)

Open `http://127.0.0.1:8765/` after starting the demo. **THEATRE** is an
illustrative orthographic globe drawn on a canvas with no basemap, token or map
service, plus fact tables of the tracks and coverage in view. **DESK** lists the
observed transits of the synthetic count line per hour: an outage shows
*Unknown*, never 0; a partial count shows *at least n* with its reason; every
count shows its coverage, crossings, evidence batches, bracketing observations,
timestamps and source label. Rendering presets change only what the globe draws.

| Endpoint (loopback) | Returns |
|---|---|
| `GET /api/v0/health` | process health |
| `GET /api/v0/snapshot?bbox=&start=&end=&layers=` | tracks and coverage for a bounded area and interval (default: the latest hours with coverage) |
| `GET /api/v0/transits?start=&end=&line=` | transit counts for one versioned line, with cited crossings and coverage |
| `GET /api/v0/stream` (WebSocket) | `subscribe` → snapshot with cursor → sequenced deltas; `resync_required` on reconnect, gap or expired cursor |

Every limit is in the `[api]` table: interval length, rows, response bytes,
query time, database connections, live sockets and a hard per-client outbound
byte cap (a slow client is disconnected). See
[ADR 0004](docs/adr/0004-package-3-browser-api.md).

## Layout

```text
backend/eye/api/      HTTP and WebSocket API, feed queries, auth port
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
schemas/              versioned wire schema (eye.wire/2; v1 kept unchanged)
scripts/              setup, verify, demo, boundary check, fixture generators
tests/                tests and synthetic fixtures
web/                  browser client: THEATRE globe and DESK panel
```

## Limitations

- THEATRE is a 2D-canvas orthographic globe without a basemap, terrain or
  imagery; it is illustrative, not a measurement surface. The CesiumJS renderer,
  presets with the floating-origin pipeline, event-claim storage (4c) and every
  real adapter are later packages. The snapshot carries no events yet.
- The server is standard-library HTTP with a small WebSocket implementation,
  sized for a single-user loopback demo; the private gateway, shared login and
  live revocation proofs belong to the private integration (Package 8).
- A passing CI run shows the public code builds and its synthetic tests pass. It
  is **not** evidence that EYE is installed or secure on the owner's private
  server; that is proved separately (brief §8).
- Dependency and image pins are listed in [`docs/dependencies.md`](docs/dependencies.md).
