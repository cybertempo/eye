#!/usr/bin/env bash
# One-command setup. Runs on: developer laptop, CI.
# Creates .venv with hash-locked developer tools, installs the locked web
# toolchain, builds the browser client and fetches the headless browser build
# pinned by the locked Playwright version (for tests/test_browser.py). Downloads
# from PyPI, npm, Docker Hub and Playwright's browser CDN only; makes no provider
# calls and needs no credentials. Nothing here opens a browser window.
set -euo pipefail
cd "$(dirname "$0")/.."

pick_python() {
  for candidate in "${EYE_PYTHON:-}" python3.12 python3.13 python3.11 python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$candidate"
      return 0
    fi
  done
  echo "setup: Python 3.11 or newer is required (set EYE_PYTHON to choose one)" >&2
  return 1
}

PYTHON="$(pick_python)"
command -v npm >/dev/null 2>&1 || { echo "setup: Node.js 22+ with npm is required" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "setup: Node.js 22+ with npm is required" >&2; exit 1; }
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' || {
  echo "setup: Node.js 22 or newer is required (found $(node --version))" >&2
  exit 1
}

echo "setup: creating .venv with $("$PYTHON" --version)"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --no-input --quiet \
  --require-hashes --no-deps --only-binary=:all: -r requirements/dev.lock

echo "setup: installing locked web toolchain"
npm ci --prefix web --ignore-scripts --no-audit --no-fund
npm run --prefix web build

# Hash-checked runtime wheels for the demo image (pure Python), so the image
# build installs offline and never reaches PyPI itself.
echo "setup: downloading hash-locked runtime wheels for the demo image"
rm -rf build/wheels
.venv/bin/python -m pip download --disable-pip-version-check --no-input --quiet \
  --require-hashes --no-deps --only-binary=:all: -r requirements/runtime.lock -d build/wheels

# The headless Chromium build matching the locked Playwright release. Skipped
# where a matching build is preinstalled (PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1).
if [ "${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-}" = "1" ]; then
  echo "setup: PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1; using the preinstalled headless browser"
else
  echo "setup: fetching the headless browser for the locked Playwright release"
  .venv/bin/python -m playwright install --only-shell chromium
fi

# The pinned PostGIS image for database tests (scripts/test-db.sh). Pulling it
# here keeps scripts/verify.sh free of network access.
POSTGIS_IMAGE="$(grep -o 'postgis/postgis:[^"]*' scripts/test-db.sh | head -n 1)"
if command -v docker >/dev/null 2>&1; then
  echo "setup: pulling $POSTGIS_IMAGE"
  docker pull --quiet "$POSTGIS_IMAGE"
else
  echo "setup: Docker not found; database tests will need EYE_TEST_DATABASE_URL" >&2
fi

echo "setup: done. Next: scripts/verify.sh"
