#!/usr/bin/env bash
# Build the dev image and check it serves the synthetic demo on host loopback.
# Runs on: developer laptop (with Docker), CI.
set -euo pipefail
cd "$(dirname "$0")/.."

compose=(docker compose -f deploy/dev/compose.yaml)
cleanup() { "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${compose[@]}" build
"${compose[@]}" up -d
port="${EYE_DEMO_PORT:-8765}"
published="$("${compose[@]}" port eye-demo 8765)"
case "$published" in
  127.0.0.1:*) echo "container-smoke: published on $published (loopback)" ;;
  *) echo "container-smoke: REFUSED, port published on $published" >&2; exit 1 ;;
esac
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${port}/api/v0/health"; then
    echo
    curl -fsS -o /dev/null "http://127.0.0.1:${port}/api/v0/snapshot"
    echo "container-smoke: health and snapshot OK on 127.0.0.1:${port}"
    exit 0
  fi
  sleep 1
done
"${compose[@]}" logs
echo "container-smoke: FAILED" >&2
exit 1
