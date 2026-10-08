#!/usr/bin/env bash
# Disposable PostGIS for tests. Runs on: developer laptop (Docker), CI.
#   scripts/test-db.sh run <command...>
# Starts the pinned image on a random host-loopback port with a password
# generated for this run only, exports EYE_TEST_DATABASE_URL to <command>,
# and removes the container afterwards, whatever the command's result.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="postgis/postgis:17-3.5@sha256:01a6a70e41e6c4467c8f55f6063555ed72db2d6662cd0d571040d42eadaeb6f6"

[ "${1:-}" = "run" ] && [ "$#" -ge 2 ] || { echo "usage: scripts/test-db.sh run <command...>" >&2; exit 2; }
shift
command -v docker >/dev/null 2>&1 || {
  echo "test-db: UNVERIFIED, Docker is not available; set EYE_TEST_DATABASE_URL to a disposable PostGIS instead" >&2
  exit 1
}

name="eye-test-db-$$-$RANDOM"
POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export POSTGRES_PASSWORD

cleanup() {
  local previous_status=$?
  trap - EXIT
  if ! docker rm -f "$name" >/dev/null 2>&1; then
    echo "test-db: cleanup FAILED; container $name may still be running" >&2
    exit 1
  fi
  exit "$previous_status"
}
trap cleanup EXIT

# The data directory lives in memory and counts towards the memory limit. 2 GiB
# leaves headroom for scripts/verify.sh's parallel test workers (about 0.9 GiB
# at peak with four) without letting the kernel kill the server mid-run.
docker run -d --name "$name" -e POSTGRES_PASSWORD -p 127.0.0.1::5432 \
  --tmpfs /var/lib/postgresql/data:rw,size=1g --memory 2g "$IMAGE" >/dev/null
port="$(docker port "$name" 5432/tcp | head -n 1 | cut -d: -f2)"
ready=""
for _ in $(seq 1 60); do
  # The image restarts once after initialisation; wait for the final server.
  if docker exec "$name" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 &&
    docker exec "$name" psql -U postgres -h 127.0.0.1 -tAc 'SELECT 1' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ -n "$ready" ] || { docker logs "$name" >&2; echo "test-db: database did not become ready" >&2; exit 1; }

export EYE_TEST_DATABASE_URL="postgresql://postgres:${POSTGRES_PASSWORD}@127.0.0.1:${port}/postgres"
unset POSTGRES_PASSWORD
"$@"
