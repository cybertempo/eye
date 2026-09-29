# ADR 0004: Package 3 browser and API

Date: 2026-09-28. Status: proposed (awaiting owner review).

## Decisions

1. **Database-backed, read-only, bounded API.** `serve` needs a reachable,
   migrated database and refuses to start without one (exit 2 or 4). API
   sessions are `READ ONLY` with a statement timeout (`api.query_timeout_ms`)
   and come from a small pool (`api.db_pool_size`); the API cannot write ingest
   history. Every query is bounded by area, interval (`api.max_interval_hours`)
   and row limits (`max_tracks`, `max_points`, `max_coverage`, `max_counts`,
   `max_crossings`). A request past a limit is **refused** (HTTP 413 or 400 with
   a wire error), never silently truncated. Responses stay under
   `server.max_response_bytes`. A database that cannot be read gives a 503
   whose text says the data is unknown; the browser shows *Unknown*, never an
   empty or zero result.
2. **One wire schema, one addition.** Snapshots, deltas, resync notices and
   subscribe messages are the existing `eye.wire/1` messages. Transit counts
   needed a message the schema did not have, so `TransitsMessage` (`GET
   /api/v0/transits`) was **added to `eye.wire/1`** without changing any
   existing message: every previously valid message is still valid. Counts have
   three closed forms (`qualified` exact, `partial` lower bound with reason,
   `unknown`/`failed` with null numbers and reason); crossings carry
   `estimated_time` with its method and the observed window. Generated browser
   types, both runtime validators and the reference library agree on new
   corpus cases. *Owner review:* whether this addition should instead start
   `eye.wire/2`.
3. **Cursors from a commit-ordered change log (migration 0003).**
   `eye.feed_change` gets one row, by trigger, when a capture batch settles and
   when a derivation run is recorded. The trigger holds a `SHARE ROW EXCLUSIVE`
   lock until commit, so change numbers become visible in commit order and a
   reader can never later discover an earlier change. A cursor is
   `e<epoch>:<change>`; the random per-database epoch (`eye.feed_epoch`) makes a
   cursor from another or a rebuilt database recognisably foreign. A snapshot
   reads its cursor and rows in one repeatable-read transaction.
4. **Snapshot, sequenced deltas, fresh snapshot on reconnect or gap** (brief
   §5). After `subscribe` the client gets a snapshot with its cursor, then
   deltas numbered 1, 2, … each naming the cursor it follows. The browser
   treats a wrong `previous_cursor` or sequence as a gap: it stops applying
   deltas and subscribes again with its last good cursor. A reconnecting client
   sends that cursor and receives `resync_required` (`reconnect` if it is a
   valid cursor of this database, `expired_cursor` otherwise) and a fresh
   snapshot; state is rebuilt from the database, not patched across the gap.
   The server sends `resync_required` (`gap`) plus a snapshot to a subscriber
   more than `api.max_pending_changes` behind, and (`overflow`) when a delta
   would break a limit. A derivation run produces an empty delta that advances
   the cursor; the browser then re-reads the transit counts.
5. **Hard cap for slow WebSocket clients.** Each socket has one outbound queue
   whose byte count, including the frame being written, may not exceed
   `api.ws_max_buffer_bytes` (at least one full message); the kernel send buffer
   is set to 32 KiB (Linux reports double) and each write has a timeout. A client
   that cannot keep up is **disconnected**, not buffered. Inbound frames are
   masked, unfragmented text of at most `api.ws_max_inbound_bytes`; anything
   else closes the socket with the matching close code. Live sockets are capped
   (`api.max_websockets`) separately from HTTP connections. The browser also
   bounds its own state and resynchronises past the limit.
6. **Browser boundary.** The Host header must be loopback; a WebSocket Origin
   must equal the request's own origin, or `api.allowed_origin` if set (for the
   later private gateway). Content-Security-Policy stays `default-src 'none'`
   with same-origin scripts, styles and connections only; untrusted text is
   inserted with `textContent`. Every outgoing body is validated; a schema
   violation is replaced by a 500 error, never sent.
7. **Authentication.** Every data request and every WebSocket handshake goes
   through the configured `AuthPort`; open sockets are re-authorised every 30 s
   (within the brief's 60 s rule) and closed with 1008 when access ends. Demo
   mode uses the local synthetic demo adapter only. Production now serves when,
   and only when, the private adapter loads and the database is migrated; there
   is still no fallback to demo auth and no login page in this repository. The
   real adapter, gateway and revocation proofs are Package 8.
8. **Token-free THEATRE globe.** THEATRE is an orthographic globe drawn by
   project code on a 2D canvas: graticule, requested area, count line and
   tracks, with keyboard and button controls. It needs no basemap, token or map
   service. This departs from the brief's CesiumJS baseline for now: CesiumJS
   adds a large, separately licensed dependency tree, WebGL in CI and the
   floating-origin and eclipse requirements of §3, which belong to the later
   rendering package. The globe is labelled illustrative, not a measurement
   surface. *Owner review:* when to adopt CesiumJS.
9. **Facts independent of rendering.** Low, Balanced and High presets change
   only graticule density, canvas resolution and track sampling. Fact tables
   (tracks, coverage, counts, crossings) are pure functions of validated
   messages (`web/src/facts.ts`); a browser test compares them across presets
   and checks, as a control, that the pictures do differ.
10. **DESK shows how each count was obtained.** For every interval: state in
    words (not colour alone), totals, uncertainty counts, reason, cited
    coverage and crossing IDs; for every crossing: estimated time and method,
    observed window, position, evidence batches and bracketing observations;
    for every coverage row: batch, state, reason and EYE receipt time; plus
    source label, synthetic marking, line and version, algorithm and run. An
    outage reads *Unknown (no count)*; a partial count reads *at least n*. The
    browser refuses a count whose total is not inbound + outbound.
11. **Demo data and container.** A new invented AIS scenario (`ais/demo`) spans
    four hours: exact, partial, outage and measured zero. `db-prepare-demo`
    migrates, loads and derives in one step. The demo container runs with a
    disposable PostGIS container whose network namespace it shares, so the
    database is on loopback with no published port; the web port is published
    on host loopback only; the one-run database password is generated by
    `scripts/demo.sh` or `scripts/container-smoke.sh` and data lives in tmpfs.
12. **Browser tests are headless and self-contained.** Playwright (pinned)
    starts a headless Chromium inside each test module and closes it; no window
    opens. Tests cover valid and invalid messages, dropped deltas, reconnects,
    unavailable and inconsistent responses, keyboard use and basic
    accessibility structure, each with a control; the accessibility checks are
    themselves shown to catch a deliberately broken page.

13. **Review repairs before merge (O44–O47).**
    - *O44, no truncated delta:* a delta is built only from the records its
      batch changed inside the subscription's area, interval and layers. If
      that is more than one delta may carry (the configured track limit, and
      the schema's 500 items), or the delta fails validation or the size
      limit, the server sends `resync_required` (`overflow`) with the
      **unchanged** cursor and a fresh snapshot. The cursor advances only
      after a complete, valid delta is queued.
    - *O45, change-feed failure:* if the change log cannot be read, every
      live subscriber is sent one 503 error saying its data may be stale. The
      browser marks the page stale (status, `role="alert"` banner, dimmed
      tables) and ignores deltas. When the feed is readable again each such
      subscriber gets `resync_required` (`gap`) and a fresh snapshot before any
      delta, and the page returns to live.
    - *O46, subscription needs a baseline:* a subscription becomes active only
      once its snapshot has been built, validated, found within the size limit
      and queued. A refused or oversized snapshot, or a refused new request,
      leaves no active subscription (and ends the previous one), so no delta
      can follow an error.
    - *O47, track ids:* a track id is `trk-` plus the first 128 bits of
      SHA-256 over the JSON pair `[source, record]`: stable across snapshots,
      deltas and reconnects, and never a truncation of the readable name, so
      two long, similar record ids cannot share an id. The fact table shows
      the source record beside the id.

14. **Review repairs before merge (O48, O49).**
    - *O48, corrections that leave the view:* a delta considers every record
      its batch touched that has, or had, any version inside the
      subscription. If such a track no longer has a current point in view,
      the delta cannot say so (deltas only upsert), so the server sends
      `resync_required` (`overflow`) with the **unchanged** cursor and a fresh
      snapshot without the track. A correction that stays in view is an
      ordinary upsert of the whole track.
    - *O49, not current until a snapshot:* once a view is on the page, every
      state but live (connecting, resynchronising, reconnecting, stale,
      failed) shows the stale banner. The page becomes live only on a snapshot
      that passed validation, or a delta that follows one in sequence; a
      refused snapshot leaves the banner up.

## Consequences

- Snapshots carry no events yet; the event-claim ledger is Package 4c.
- The standard-library server with threads per connection suits a loopback,
  single-user demo; capacity for the private deployment is measured there.
- The threat model document (brief §5) is still to be written; this package
  implements and tests the slow-client, malformed-message, revoked-session and
  foreign-origin cases it will list.
- A green CI run shows the public code and synthetic demo work. It is not
  evidence of the private installation, gateway or login.
