#!/usr/bin/env bash
# Build the dev image, start it with its disposable PostGIS database and check
# it serves the database-backed synthetic demo on host loopback.
# Runs on: developer laptop (with Docker), CI.
set -euo pipefail
cd "$(dirname "$0")/.."

# A password for this run only; it is never written to disk.
EYE_DEMO_DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export EYE_DEMO_DB_PASSWORD
compose=(docker compose -f deploy/dev/compose.yaml)
cleanup() {
  local previous_status=$?
  trap - EXIT
  if ! "${compose[@]}" down --remove-orphans; then
    echo "container-smoke: cleanup FAILED; the demo containers may still be running" >&2
    exit 1
  fi
  exit "$previous_status"
}
trap cleanup EXIT

"${compose[@]}" build
"${compose[@]}" up -d
port="${EYE_DEMO_PORT:-8765}"
published="$("${compose[@]}" port db 8765)"
case "$published" in
  127.0.0.1:*) echo "container-smoke: published on $published (loopback)" ;;
  *) echo "container-smoke: REFUSED, port published on $published" >&2; exit 1 ;;
esac
base="http://127.0.0.1:${port}"
for _ in $(seq 1 90); do
  if curl -fsS "${base}/api/v0/health"; then
    echo
    # Event ledger: sourced reports and review candidates, never mixed up.
    curl -fsS "${base}/api/v0/snapshot" | python3 -c '
import json, sys
events = json.load(sys.stdin)["events"]
standing = sorted((e["case_id"], e["standing"]) for e in events)
candidates = {e["case_id"] for e in events if e["standing"] == "review_candidate"}
ok = len(events) == 7 and candidates == {"SYN-SL-1", "SYN-GAP-21", "SYN-TS-1"}
sys.exit(0 if ok else f"container-smoke: unexpected event cases {standing}")
'
    # News and media: separate items, unknown-rights headlines withheld,
    # syndication a suggestion only (Package 4d).
    curl -fsS "${base}/api/v0/snapshot" | python3 -c '
import json, sys
view = json.load(sys.stdin)
media = {m["item_id"]: m for m in view["media"]}
shown = [v["headline"] for m in view["media"] for v in m["versions"] if v["rights"]["status"] == "unknown"]
bases = {s["basis"] for s in view["media_suggestions"]}
ok = len(media) == 8 and shown == [None] and "syndicated_copy" in bases
sys.exit(0 if ok else f"container-smoke: unexpected news items {sorted(media)} {shown} {bases}")
'
    # The four demo hours: exact, partial, outage (unknown, never zero), measured zero.
    curl -fsS "${base}/api/v0/transits" | python3 -c '
import json, sys
counts = json.load(sys.stdin)["counts"]
states = [(c["state"], c["total"]) for c in counts]
expected = [("qualified", 1), ("partial", 1), ("unknown", None), ("qualified", 0)]
sys.exit(0 if states == expected else f"container-smoke: unexpected transit counts {states}")
'
    echo "container-smoke: health, snapshot, event cases, news items and transit counts OK on 127.0.0.1:${port}"
    exit 0
  fi
  sleep 1
done
"${compose[@]}" logs
echo "container-smoke: FAILED" >&2
exit 1
