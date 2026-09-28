#!/usr/bin/env bash
# One-command setup. Runs on: developer laptop, CI.
# Creates .venv with hash-locked developer tools, installs the locked web
# toolchain and builds the demo page. Downloads packages from PyPI and npm only;
# makes no provider calls and needs no credentials.
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

echo "setup: creating .venv with $("$PYTHON" --version)"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --no-input --quiet \
  --require-hashes --no-deps --only-binary=:all: -r requirements/dev.lock

echo "setup: installing locked web toolchain"
npm ci --prefix web --ignore-scripts --no-audit --no-fund
npm run --prefix web build

echo "setup: done. Next: scripts/verify.sh"
