# EYE

EYE is a planned self-hosted geospatial and solar-system observation and research
application. This repository holds its public source. **Status: Packages 0
to 3, the source-independent parts of 4c (world-event ledger) and 4d (news and
media evidence), and Package 5: the lifecycle slice (rollups, manifests,
checked retention and a synthetic backup and restore drill) and the research
slice (coverage-weighted baselines and a backtester).**
It contains the layout, tooling, CI, the
versioned wire schema with validation on both sides, PostgreSQL/PostGIS storage
for captures, evidence, observations and coverage, a synthetic vessel adapter
with track splitting, crossings of a versioned synthetic count line and
coverage-aware transit counts, and a database-backed API (bounded REST snapshots
and a WebSocket feed of sequenced deltas) serving a browser client with a
token-free THEATRE globe and a DESK observed-transit panel, and an append-only
event-claim ledger whose sourced reports, review candidates, corrections and
conflicts the API and browser show. All data is invented. No real data feed or
event-report adapter, real Golden Gate geometry, basemap, CesiumJS renderer,
private installation or AI feature exists yet.

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
| Developer laptop, CI | `scripts/verify.sh` | Boundary scan, lint, format, generated-type check, a clean typechecked rebuild of `web/dist` from `web/src` plus a check that it matches a separate fresh build (so the browser tests never run stale JavaScript), config check and all tests: PostGIS tests in a disposable loopback container and headless browser tests (no window opens), run in four parallel workers (`EYE_TEST_WORKERS=0` runs them serially). No network. Fails (never skips) if no database is available. |
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
synthetic fixtures. The database records whether it is the demo or a private
installation: `db-migrate` and `db-prepare-demo` claim it once, and the demo
and every other command need that claim before they serve or write. The demo
refuses a database claimed `private` or holding a non-synthetic capture;
production refuses one not claimed `private`
([ADR 0009](docs/adr/0009-demo-database-identity.md)). `db-derive-transits` counts crossings of every line in
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

## World events (Package 4c, source-independent part)

Snapshots and deltas carry **event cases** from an append-only claim ledger
(migration 0005, wire `eye.wire/3`, now served as `eye.wire/4`). Each case keeps every version its source
published, with the reported event location at its stated precision (a point,
a road segment with its affected direction, or an area), event time and
uncertainty, evidence reference and receipt batches. DESK's *world events*
table shows, in separate columns, the reported location and a linked track's
last observed position; THEATRE draws the two as separate marks.

- Only a sourced report (official or operator, citing evidence) can name an
  accident, casualty or collision. A lost flight signal, an AIS gap, a stopped
  vessel or a traffic slowdown is a *review candidate* that reports nothing;
  the capture parser, a database constraint and the wire schema each refuse
  anything else.
- Corrections are later versions; claims published together that disagree are
  *unresolved* until a later claim settles them. Duplicate deliveries add
  receipts, not claims, and replay re-derives every claim.
- A case links to a track only when exactly one named track was observed in
  its window near the reported location; otherwise it stays unlinked, with the
  reason.
- Where no event-report source covered part of the view, event coverage is
  *unknown*, never "no events"; a source covering only part of the view's
  area is *partial*, not a measured zero, and live coverage that narrows a
  gap brings a fresh snapshot.
- A case is shown when its event time, widened by its stated uncertainty,
  overlaps the view's interval.
- Event-report counts in DESK are for the selected view (its area, interval
  and current versions), never the whole capture's count.

The only event source is `synthetic-events`, invented fixtures in
`tests/fixtures/synthetic/events/`. See
[ADR 0005](docs/adr/0005-package-4c-event-ledger.md).

## News and media evidence (Package 4d, provider-neutral part)

Snapshots carry **news and media items** (migration 0006, wire `eye.wire/4`),
kept apart from event cases. Each item is one article, image or video with
every version its source published: first-publication and revision times,
EYE's receipt time, a link to the original, publisher and creator, the
headline (untrusted text), reuse rights and an optional place. EYE stores
metadata and a link only, never the work itself. DESK's *news and media
evidence* table shows each item's source, age, delivery delay, place role,
rights and history.

- An item is evidence that it was published, never a verified account. An
  image or video's capture time is its creator's claim; nothing is labelled
  live, and old footage is not drawn in a current view.
- Only a source-stated event place is drawn on THEATRE, as an approximate
  area. A publisher's city, a mentioned place and an automated geocode are
  listed but never drawn.
- An item whose reuse rights are unknown is a link only: its headline is
  never stored, in raw evidence or anywhere else. Raw evidence is permanent,
  so a news capture is validated in full (reviewed envelope keys and every
  item's strict parse) before anything is archived, and refused whole on any
  failure.
- Exact repeat deliveries add receipts, not items. Related items across
  publishers, and syndicated copies, are listed as *suggestions*; nothing is
  merged or counted as independent confirmation.
- A news source's outage is *failed* coverage, and time no news source
  covered is *unknown*, never "no news".

The only news source is `synthetic-news`, invented fixtures in
`tests/fixtures/synthetic/media/`. Candidate providers are listed in
[`docs/news-media-source-shortlist.md`](docs/news-media-source-shortlist.md);
none is approved or enabled. See
[ADR 0006](docs/adr/0006-package-4d-news-media-evidence.md).

## Lifecycle: rollups, manifests, retention and a synthetic drill (Package 5, partial)

Every command below runs on the **developer laptop** (or in CI, through the
tests) against a loopback database with synthetic data. None of them needs an
account or the owner's server, and none of them opens a browser.

| Where it runs | Command | What it does |
|---|---|---|
| Developer laptop | `… -m eye db-rollup --config config/eye.example.toml` | Records the rollups and their manifests for every partition (source, layer, UTC day) |
| Developer laptop | `… -m eye db-manifest-check --config …` | Re-derives every current manifest and records `valid`, `stale`, `failed` or `unverified`; exits 5 unless all are valid |
| Developer laptop | `EYE_BACKUP_DIR=<empty temp dir> … -m eye db-backup --config …` | Writes a **synthetic** backup generation to that directory and verifies it by an independent read-back |
| Developer laptop | `EYE_BACKUP_DIR=… EYE_RESTORE_DATABASE_URL=<empty loopback db> … -m eye db-restore-drill --generation ID --config …` | Restores that generation into the empty database and compares every named metric |
| Developer laptop | `… -m eye db-retention-plan --config …` | Read-only: each partition's retention verdict and its reasons |
| Developer laptop | `… -m eye db-retention-execute --partition SOURCE:LAYER:YYYY-MM-DD --config …` | Refused unless `retention.allow_deletion = true` (default false), in demo mode, for a synthetic source |
| Developer laptop (Docker), CI | `scripts/restore-drill.sh` | The whole synthetic drill in two disposable databases and a temporary directory |

(`…` is `PYTHONPATH=backend .venv/bin/python`, with `EYE_DATABASE_URL` set as
above.)

- **Rollups never turn unknown into zero.** An hour lacking usable coverage
  has no value, a partial one is a lower bound, and only a fully covered hour
  is exact; a covered hour with nothing seen is a real 0. Coverage is judged
  over the whole area requested that day: a qualified capture of one area
  never vouches for a failed or missing capture of another at the same time. Activity per grid
  cell names the cell and its bounds, never a mean or centre position.
- **Manifests** record each rollup's inputs (batches, checksum, watermark),
  versions, outputs and coverage. A late arrival or correction makes a
  manifest stale; the next `db-rollup` adds a new one and keeps the old.
- **Retention prunes raw evidence bytes only, and only after every check
  passes**: the lateness window (at least 48 hours) has closed, every
  manifest re-derives exactly, the partition's own bytes still reproduce
  every observation, event claim, media item, receipt and coverage row they
  produced (`ledger-replay`), and a verified backup holds the same bytes and
  is re-read just before pruning. A check that cannot run is UNVERIFIED and
  blocks. The database independently refuses pruning before the lateness
  window closes by its own clock, without current, complete manifests checked
  valid in the same transaction, or when a batch's ledger rows no longer match
  the seal it recorded when the batch settled. Observation, event-claim and media
  history is never deleted. After pruning, `db-manifest-check` reports that
  partition's `ledger-replay` as UNVERIFIED: only the restore drill can
  replay those bytes.
- **The backup and drill are a synthetic code test.** They prove nothing
  about an off-site Google Drive backup, the private installation or any
  recovery objective.

See [ADR 0007](docs/adr/0007-package-5-lifecycle.md).

## Research: coverage-weighted baselines and a backtester (Package 5)

Every command below runs on the **developer laptop** (or in CI, through the
tests) against a loopback database with synthetic data, after `db-rollup`.
Nothing here calls a model, a provider or the network.

| Where it runs | Command | What it does |
|---|---|---|
| Developer laptop | `… -m eye db-backtest --series SOURCE:LAYER:DERIVATION[:SCOPE] --start YYYY-MM-DD --end YYYY-MM-DD --config …` | Checks every rollup manifest it will cite, backtests each target bin against the same bin on earlier days, and records the run; exits 5 if a manifest is stale |
| Developer laptop | `… -m eye db-backtest-replay --run ID --config …` | Recomputes a recorded run from the manifests it cites and compares every result; exits 5 on any difference |

(`…` is `PYTHONPATH=backend .venv/bin/python`, with `EYE_DATABASE_URL` set as
above. Series are `observations-hourly` for a position source, or
`transit-daily:line:<id>/v<n>` for `synthetic-ais` vessels. Parameters and
bounds are in the `[research]` table.)

- **A gap is never a zero.** Only hours that are exactly covered count. An
  uncovered, failed, partial or missing hour adds nothing to the baseline, and
  a target bin without enough coverage, or without enough covered history,
  *abstains* with a reason code and no value.
- **Results are facts.** Each bin is `detected`, `not_detected` or
  `abstained`, with numbers and a fixed reason code; nothing writes prose
  about a result. A detection means "unusually high against its own recent
  history", not a cause or a forecast.
- **Replayable.** A run records the exact manifests it read. A late arrival
  changes nothing already recorded: it makes those manifests stale, the next
  `db-rollup` adds new ones, and the next backtest is a new run. Replaying an
  old run gives identical results.
- **Bounded.** At most `max_days` target days and `max_output_rows` results;
  larger requests are refused, never cut short.

See [ADR 0008](docs/adr/0008-package-5-baselines-backtester.md).

## Layout

```text
backend/eye/api/      HTTP and WebSocket API, feed queries, auth port
backend/eye/ingest/   capture pipeline and synthetic AIS adapter
backend/eye/storage/  database connection and migration runner
backend/eye/wire/     wire-schema runtime validator
backend/eye/worker/   tracks, line crossings, transit counts, rollups, retention, synthetic backup, baselines, backtester
backend/eye/raster/   optional imagery pipeline (Package 6)
config/               example configuration
deploy/dev/           loopback synthetic demo container
deploy/lab/           generic private-deployment templates (placeholder)
docs/                 brief, source-policy register, dependencies, ADRs
migrations/           forward-only PostgreSQL/PostGIS migrations
reference/lines/      versioned synthetic count lines
requirements/         hash-locked Python tool manifests
schemas/              versioned wire schema (eye.wire/4; v1 to v3 kept unchanged)
scripts/              setup, verify, demo, restore drill, boundary and web-build checks, fixture generators
tests/                tests and synthetic fixtures
web/                  browser client: THEATRE globe and DESK panel
```

## Limitations

- THEATRE is a 2D-canvas orthographic globe without a basemap, terrain or
  imagery; it is illustrative, not a measurement surface. The CesiumJS renderer,
  presets with the floating-origin pipeline and every real adapter (including
  aviation, marine and road occurrence-report sources for 4c) are later
  packages. Review candidates are ingested as claims; no worker yet derives
  them from tracks.
- The server is standard-library HTTP with a small WebSocket implementation,
  sized for a single-user loopback demo; the private gateway, shared login and
  live revocation proofs belong to the private integration (Package 8).
- A passing CI run shows the public code builds and its synthetic tests pass. It
  is **not** evidence that EYE is installed or secure on the owner's private
  server; that is proved separately (brief §8).
- Package 5 runs on synthetic data only: rollups and backtest results are not
  shown in the browser, the detector's false detection rate is measured only on
  invented series, and the only backup target is a synthetic local directory.
  Production refuses `retention.allow_deletion`; deleting real data still needs
  database role separation and a proven real backup and restore on the private
  server.
- Dependency and image pins are listed in [`docs/dependencies.md`](docs/dependencies.md).
