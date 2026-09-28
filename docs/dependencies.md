# Dependencies and pins

The demo server uses the Python standard library only. Database code uses
pg8000 (pure Python). Everything else is a developer, CI or container tool,
pinned as below.

| What | Pinned in | Pin |
|---|---|---|
| Python runtime (pg8000 and its dependencies) | `requirements/runtime.in` → `requirements/runtime.lock` | exact versions with SHA-256 hashes |
| Python dev tools (pytest, ruff, jsonschema for tests), plus runtime | `requirements/dev.in` → `requirements/dev.lock` | exact versions with SHA-256 hashes |
| CI audit tool (pip-audit) | `requirements/audit.in` → `requirements/audit.lock` | exact versions with SHA-256 hashes |
| TypeScript | `web/package.json` → `web/package-lock.json` | exact version and integrity hashes |
| Test database image | `scripts/test-db.sh` | `postgis/postgis:17-3.5` by index digest (PostgreSQL 17.5, PostGIS 3.5.2; linux/amd64 only) |
| Demo base image | `deploy/dev/Dockerfile` | `python:3.12-slim-bookworm` by index digest (Python 3.12.14 when pinned) |
| Secret scanner | `.github/workflows/ci.yml` | `ghcr.io/gitleaks/gitleaks` v8.30.1 by index digest |
| GitHub Actions | `.github/workflows/ci.yml` | full commit SHAs with the tag in a comment |
| CI runner | `.github/workflows/ci.yml` | `ubuntu-24.04` label (GitHub-managed; cannot be digest-pinned) |

Tests in `tests/test_deploy_pins.py` fail if a Dockerfile `FROM`, the test
database image, a workflow action or a workflow `docker run` image is not
pinned by digest or commit.

## Updating a pin

All update commands run on the **developer laptop**, then the change goes
through a reviewed PR. Review the upstream changelog and licence before changing
a pin, update `NOTICE.md` if the licence or package set changes, and rerun
`scripts/setup.sh` and `scripts/verify.sh --container` (developer laptop), then
let CI run the full workflow.

- Python tools: edit the `.in` file, then regenerate the lock with
  [uv](https://docs.astral.sh/uv/):
  `uv pip compile --universal --python-version 3.11 --generate-hashes --no-header requirements/dev.in -o requirements/dev.lock`
  (same for `runtime` and `audit`; regenerate `dev` after `runtime`, since it
  includes it).
- TypeScript: edit the exact version in `web/package.json`, then
  `npm install --prefix web --package-lock-only --ignore-scripts`.
- Base image: `docker pull python:3.12-slim-bookworm`, then
  `docker image inspect python:3.12-slim-bookworm --format '{{index .RepoDigests 0}}'`
  and copy the digest into the `FROM` line.
- PostGIS image: `docker buildx imagetools inspect postgis/postgis:<tag>` and copy
  the index digest into `scripts/test-db.sh`.
- gitleaks image: `docker buildx imagetools inspect ghcr.io/gitleaks/gitleaks:<tag>` and
  copy the index digest.
- Actions: `git ls-remote https://github.com/<owner>/<action> refs/tags/<tag>` and
  replace the SHA and the tag comment.
