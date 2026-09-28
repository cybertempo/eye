#!/usr/bin/env bash
# Start the synthetic demo on loopback. Runs on: developer laptop.
# Prints the URL; it never opens a browser. Stop with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "demo: run scripts/setup.sh first" >&2; exit 1; }
exec env PYTHONPATH=backend .venv/bin/python -m eye serve --config "${EYE_CONFIG:-config/eye.example.toml}"
