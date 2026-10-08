#!/usr/bin/env bash
# Synthetic backup and restore drill. Runs on: developer laptop (Docker), CI.
# SYNTHETIC CODE TEST ONLY: it loads the invented demo data into a disposable
# PostGIS, derives transits and rollups, writes a backup generation to a
# temporary directory, verifies it by an independent read-back, restores it
# into a second, empty disposable database and compares the named metrics.
# It does not prove an off-site Google Drive backup, the private installation
# or any recovery objective, and it never prunes anything.
# Uses the server in EYE_TEST_DATABASE_URL if set, otherwise scripts/test-db.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "restore-drill: run scripts/setup.sh first" >&2; exit 1; }
if [ -n "${EYE_TEST_DATABASE_URL:-}" ]; then
  PYTHONPATH=backend .venv/bin/python scripts/restore_drill.py
else
  scripts/test-db.sh run env PYTHONPATH=backend .venv/bin/python scripts/restore_drill.py
fi
