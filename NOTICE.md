# Notices

This file records third-party code, assets, models and data included in or used
by this repository, with their licences. It is not a project licence; see the
licence status in [`README.md`](README.md).

## Upstream code, assets, models and datasets in the repository

None. Package 0 imports no upstream code, media, models or datasets. Before
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
| pytest | 9.1.1 | MIT | tests |
| ruff | 0.16.9 | MIT | lint and format |
| iniconfig, packaging, pluggy, pygments, colorama | see `requirements/dev.lock` | MIT / Apache-2.0 or BSD-2-Clause / MIT / BSD-2-Clause / BSD-3-Clause | pytest dependencies |
| pip-audit and its dependencies | see `requirements/audit.lock` | Apache-2.0 (pip-audit); see lock for others | CI dependency scan only |
| TypeScript | 7.0.2 (+ platform binary packages) | Apache-2.0 | web typecheck and build |
| Python base image | `python:3.12-slim-bookworm@sha256:392307d2…` | PSF licence (Python) and Debian package licences | demo container |
| gitleaks | `ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0…` (v8.30.1) | MIT | CI secret scan |
| GitHub Actions: checkout, setup-python, setup-node, dependency-review-action | pinned commits in `.github/workflows/ci.yml` | MIT | CI |

## Data

All data in `tests/fixtures/` is invented for this repository. No real
observation, capture or provider response is included.
