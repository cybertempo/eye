# EYE

EYE is a planned self-hosted geospatial and solar-system observation and research
application. This repository holds its public source. **Status: Package 0
(repository foundation).** It contains the layout, tooling, CI and a small synthetic
demo. No real data feed, globe renderer, database or AI feature exists yet.

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
Node.js 22 with npm, and optionally Docker for the container check.

| Where it runs | Command | What it does |
|---|---|---|
| Developer laptop, CI | `scripts/setup.sh` | Creates `.venv` with hash-locked tools, runs `npm ci`, builds the demo page. Downloads from PyPI and npm only. |
| Developer laptop, CI | `scripts/verify.sh` | Boundary scan, lint, format, typecheck, config check and tests. No network. |
| Developer laptop (Docker), CI | `scripts/verify.sh --container` | Adds an image build and a loopback smoke test. |
| Developer laptop | `scripts/demo.sh` | Serves the synthetic demo on `http://127.0.0.1:8765/`. Open that URL yourself; stop with Ctrl-C. |
| Developer laptop (Docker) | `docker compose -f deploy/dev/compose.yaml up --build` | The same demo in a container, published on host loopback only. Run `scripts/setup.sh` first. |

## Configuration

All settings live in one TOML file; [`config/eye.example.toml`](config/eye.example.toml)
is the demo configuration. Set `EYE_CONFIG` to use another file with `scripts/demo.sh`.
Check a file without starting anything (developer laptop):
`PYTHONPATH=backend .venv/bin/python -m eye check-config --config <file>`.

The loader refuses, before opening a socket:

- a non-loopback `bind_host` in demo mode (exit code 2);
- any entry in `providers.enabled` (no provider has an approved
  [source-policy](docs/source-policy-register.md) row);
- unknown keys;
- production mode without the private authentication adapter (exit 2 or 3).
  Demo authentication is never a production fallback. Production serving itself
  is not built yet (exit 4 after the adapter check).

## Layout

```text
backend/eye/api/      HTTP API, auth port, demo server
backend/eye/ingest/   provider adapters (none yet)
backend/eye/worker/   derivation workers (Package 1+)
backend/eye/raster/   optional imagery pipeline (Package 6)
config/               example configuration
deploy/dev/           loopback synthetic demo container
deploy/lab/           generic private-deployment templates (placeholder)
docs/                 brief, source-policy register, dependencies, ADRs
migrations/           database migrations (Package 1)
requirements/         hash-locked Python tool manifests
schemas/              versioned wire schema (Package 1)
scripts/              setup, verify, demo, boundary check
tests/                tests and synthetic fixtures
web/                  browser client (demo page only)
```

## Limitations

- The demo serves one static synthetic snapshot as tables. THEATRE, DESK, the
  wire schema, storage and every real adapter are later packages.
- A passing CI run shows the public code builds and its synthetic tests pass. It
  is **not** evidence that EYE is installed or secure on the owner's private
  server; that is proved separately (brief §8).
- Dependency and image pins are listed in [`docs/dependencies.md`](docs/dependencies.md).
