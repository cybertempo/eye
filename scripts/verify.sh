#!/usr/bin/env bash
# One-command verification. Runs on: developer laptop, CI.
# Needs scripts/setup.sh first. Rebuilds web/dist from web/src before the
# tests, so the browser tests always run the current source. Makes no network
# calls: database tests use a disposable PostGIS container from the image setup
# pulled (scripts/test-db.sh), or the server in EYE_TEST_DATABASE_URL if set.
# Without either, verify FAILS; database tests are never silently skipped.
# Pass --container to also build the dev image and smoke-test it (needs Docker;
# the image build pulls the pinned base image).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "verify: run scripts/setup.sh first" >&2; exit 1; }
PY=.venv/bin/python

step() { echo; echo "== verify: $*"; }

step "public-repository boundary (tracked files, licence status, secrets, browser launch)"
"$PY" scripts/check_repo_boundary.py

step "lint and format"
.venv/bin/ruff check .
.venv/bin/ruff format --check .

step "generated wire types are current"
"$PY" scripts/gen_wire_types.py --check

step "generated synthetic AIS fixtures are current"
"$PY" scripts/gen_ais_fixtures.py --check

# The browser tests load web/dist, so rebuild it from the current web/src here;
# otherwise a checkout whose source changed after setup tests stale JavaScript.
# Starting from an empty directory drops output of deleted modules. tsc
# typechecks while it compiles and fails on a type error.
step "web build (typecheck and compile web/src into web/dist)"
rm -rf web/dist
npm run --prefix web build

step "web/dist matches a fresh build of web/src"
"$PY" scripts/check_web_dist.py

step "configuration: example accepted"
PYTHONPATH=backend "$PY" -m eye check-config --config config/eye.example.toml

step "tests (loopback demo, wire schema on both sides, PostGIS storage)"
export EYE_REQUIRE_DB=1
if [ -n "${EYE_TEST_DATABASE_URL:-}" ]; then
  "$PY" -m pytest
else
  scripts/test-db.sh run "$PY" -m pytest
fi

if [ "${1:-}" = "--container" ]; then
  step "container build and smoke test"
  scripts/container-smoke.sh
fi

echo
echo "verify: PASSED"
