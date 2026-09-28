# Notices

This file records third-party code, assets, models and data included in or used
by this repository, with their licences. It is not a project licence; see the
licence status in [`README.md`](README.md).

## Upstream code, assets, models and datasets in the repository

None. Packages 0 to 3 import no upstream code, media, models or datasets. The
THEATRE globe is drawn by project code; it uses no basemap, imagery, map tiles
or map-service token. Before
anything is imported, add a row here with source URL, exact commit or version,
licence, attribution text, files copied and reviewer.

| Item | Source and pinned revision | Licence | Files | Reviewed by / date |
|---|---|---|---|---|
| — | — | — | — | — |

## Tools used to build and test (not redistributed)

These are installed from their registries by `scripts/setup.sh` or used in CI.
None of their code is committed here. Licences were read from the registry
metadata on 2026-09-28.

| Tool | Version / pin | Licence | Used by |
|---|---|---|---|
| pg8000 | 1.31.5 | BSD-3-Clause | database driver (runtime; installed in the demo image from the hash-locked `requirements/runtime.lock`) |
| scramp, asn1crypto, python-dateutil, six | see `requirements/runtime.lock` | MIT-0 / MIT / Apache-2.0 or BSD-3-Clause / MIT | pg8000 dependencies |
| jsonschema and its dependencies (attrs, referencing, jsonschema-specifications, rpds-py, typing-extensions) | 4.26.0; see `requirements/dev.lock` | MIT (typing-extensions: PSF-2.0) | reference validator in tests only |
| pytest | 9.1.1 | MIT | tests |
| Playwright for Python | 1.56.0 | Apache-2.0 | headless browser tests only |
| greenlet, pyee | see `requirements/dev.lock` | MIT AND PSF-2.0 / MIT | Playwright dependencies |
| Chromium headless shell | build 1194 (Chromium 141.0.7390.37), fetched by `scripts/setup.sh` for the locked Playwright release | BSD-3-Clause and bundled third-party licences (Chromium) | headless browser tests only; not committed or redistributed |
| ruff | 0.16.9 | MIT | lint and format |
| iniconfig, packaging, pluggy, pygments, colorama | see `requirements/dev.lock` | MIT / Apache-2.0 or BSD-2-Clause / MIT / BSD-2-Clause / BSD-3-Clause | pytest dependencies |
| pip-audit and its dependencies | see `requirements/audit.lock` | Apache-2.0 (pip-audit); see lock for others | CI dependency scan only |
| TypeScript | 7.0.2 (+ platform binary packages) | Apache-2.0 | web typecheck and build |
| PostGIS image | `postgis/postgis:17-3.5@sha256:01a6a70e…` | PostgreSQL licence; PostGIS GPL-2.0-or-later; Debian package licences. Run as a separate server process; nothing from it is copied into this repository or into the EYE image. | disposable test database and demo database (`deploy/dev/compose.yaml`) |
| Python base image | `python:3.12-slim-bookworm@sha256:392307d2…` | PSF licence (Python) and Debian package licences | demo container |
| gitleaks | `ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0…` (v8.30.1) | MIT | CI secret scan |
| GitHub Actions: checkout, setup-python, setup-node, dependency-review-action | pinned commits in `.github/workflows/ci.yml` | MIT | CI |

## Data

All data in `tests/fixtures/` and `reference/lines/` is invented for this
repository, including the synthetic AIS reports and the placeholder count line.
No real observation, capture, vessel identifier, provider response or real
Golden Gate geometry is included.
