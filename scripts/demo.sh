#!/usr/bin/env bash
# Start the synthetic demo on loopback. Runs on: developer laptop.
# Default: build and run the demo container with its disposable PostGIS
# database (needs Docker). --local: run from .venv against the PostgreSQL/PostGIS
# server in EYE_DATABASE_URL (loopback only; the demo loads synthetic data into it).
# Prints the URL; it never opens a browser. Stop with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--local" ]; then
  [ -x .venv/bin/python ] || { echo "demo: run scripts/setup.sh first" >&2; exit 1; }
  [ -n "${EYE_DATABASE_URL:-}" ] || { echo "demo: --local needs EYE_DATABASE_URL" >&2; exit 1; }
  config="${EYE_CONFIG:-config/eye.example.toml}"
  PYTHONPATH=backend .venv/bin/python -m eye db-prepare-demo --config "$config"
  exec env PYTHONPATH=backend .venv/bin/python -m eye serve --config "$config"
fi

command -v docker >/dev/null 2>&1 || { echo "demo: Docker is required (or use --local)" >&2; exit 1; }
[ -f web/dist/app.js ] || { echo "demo: run scripts/setup.sh first (builds web/dist)" >&2; exit 1; }
EYE_DEMO_DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export EYE_DEMO_DB_PASSWORD
echo "demo: starting on http://127.0.0.1:${EYE_DEMO_PORT:-8765}/ (open it yourself; Ctrl-C stops and removes it)"
trap 'docker compose -f deploy/dev/compose.yaml down --remove-orphans' EXIT
docker compose -f deploy/dev/compose.yaml up --build
