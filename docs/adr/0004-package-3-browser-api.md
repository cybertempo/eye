# ADR 0004: Package 3 browser and API

Date: 2026-09-28. Status: accepted: merged to `main` in PR #4 (merge commit
`01692ee`, 2026-09-29). The wire version in decisions 2 and 16 has since moved
to `eye.wire/3` for event cases ([ADR 0005](0005-package-4c-event-ledger.md))
and to `eye.wire/4` for news and media items
([ADR 0006](0006-package-4d-news-media-evidence.md)); the rest stands.

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
2. **One wire schema, now version 2.** Snapshots, deltas, resync notices and
   subscribe messages keep their `eye.wire/1` shapes, now labelled
   `eye.wire/2` (`schemas/eye-wire.v2.schema.json`). Version 2 adds what
   version 1 cannot carry: `TransitsMessage` (`GET /api/v0/transits`) and
   tracks with contested positions (decision 15). Counts have three closed
   forms (`qualified` exact, `partial` lower bound with reason,
   `unknown`/`failed` with null numbers and reason); crossings carry
   `estimated_time` with its method and the observed window. The version-1
   file is kept exactly as merged and serves nothing (decision 16). Generated
   browser types, both runtime validators and the reference library agree on
   the version-2 corpus.
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
   deltas and subscribes again with its last good cursor, on a new connection
   (one subscribe per connection since O65, item 19). A reconnecting client
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

15. **Review repairs before merge (O50, O51).**
    - *O50, record identity includes the layer:* a source record is
      `(source, layer, record id)` everywhere. The observation id is derived
      from source, layer, record id, observed time and content; version history
      (`eye.observation_version`, migration 0004) and replay rank versions only
      within one such record; the API groups tracks by it; and the wire track
      id is `trk-` plus 128 bits of SHA-256 over `[source, layer, record]`.
      The same record id reused as a flight and a vessel is two records, two
      histories and two tracks. Earlier ids are not derivable under this rule,
      so migration 0004 refuses a database that already holds observations:
      build a new one and ingest its raw evidence again.
    - *O51, contested positions stay contested:* when the latest publications
      for one record and observed time conflict (same publication time), the
      track's `points` (its route) leave that time out and `conflicts` carries
      every claim with its observation id, publication and receipt times,
      position and evidence batches, including claims outside the view. No
      claim is joined to the route or taken as the last position; the browser
      says "Unresolved: n conflicting claims" and draws each claim as a
      separate marker. A later, unique publication supersedes all claims and
      the time rejoins the route. `Track` is now either routed (at least one
      point) or unresolved (no points, at least one conflict), and an empty
      track is still invalid. This changes what a track can be, so it is part
      of `eye.wire/2` (decision 16), not an addition to version 1.

16. **Review repairs before merge (O52 to O54).**
    - *O52, routes break at contested times:* the drawn route is split at
      every contested observed time that falls between two resolved points,
      so no line joins the positions either side of an unknown one. The
      split is a pure function of the message (`routeRuns` in
      `web/src/facts.ts`); each run is thinned separately and keeps its first
      and last point, so Low, Balanced and High break at the same times. The
      canvas records what it stroked (`data-route-runs`) for tests. A resolved
      middle time is an ordinary point and the route is one run.
    - *O53, a new wire version:* the conflict track shape (and transit counts,
      which also came with Package 3) start `eye.wire/2` rather than change
      `eye.wire/1` in place, while nothing is deployed. The version-1 schema
      stays byte for byte as merged, with its own corpus; tests show the
      unchanged version-1 validator refuses every version-2 message, and the
      new shapes even when relabelled `eye.wire/1`, while every other message
      relabelled is still valid version 1. Ingest receipts keep their
      `eye.wire/1` label: an observation's fields did not change, so replay
      of earlier evidence stays exact. On reconnect, a message in another
      version is not treated as a gap: the server answers a client in another
      version with a 400 naming its own and closes with 1003; the browser,
      given a server message in another version, stops without resubscribing
      or reconnecting, keeps the data under the stale banner and asks for a
      reload, since retrying would get the same answer.
    - *O54, the claim limit is enforced before validation:* a conflict is read
      up to 21 claims, and a claim up to 101 evidence batches. Past 20 claims
      (or 100 batches, or 100 contested times on one track) the request is
      refused with a 413 that names the record and the time, before a
      response is built: never a conflict cut short and never the generic
      500 for an invalid outgoing message. Over WebSocket the refusal leaves
      no active subscription, as for any refused snapshot (O46).

17. **Review repair before merge (O55): times compare chronologically.** Wire
    timestamps may carry 0 to 6 fractional digits, so string order is not time
    order ("03:04:00Z" sorts after "03:04:00.5Z"). The browser compares them
    with `compareTime` (`web/src/facts.ts`), which pads the fraction to six
    digits and keeps microsecond precision without floating point. It is used
    to split routes, to choose the last position and the latest conflict, to
    check count intervals and to order coverage rows. Tests cover a conflict
    at 03:04:00Z before a resolved fix at 03:04:00.5Z, and the reverse, where
    the conflict is latest, each beside a whole-second control, in every
    preset and in the DESK text.

18. **Post-merge repair (O63): a new view is not live until its snapshot.**
    After **Show interval** the page used to stay `live` and could describe
    the new window (summary, transit counts) over the previous window's
    tracks, events and coverage until the new snapshot arrived; the bounded
    state rebuild likewise cleared its data and resubscribed while still
    `live`. Now `LiveFeed.subscribe` with `fresh` set leaves `live` at once
    (status `resyncing`, stale banner) for every caller: Show interval, the
    bounded-state rebuild and the first subscription. Only a snapshot whose
    area and interval match the latest request is applied and returns the page
    to `live`; a snapshot still in flight for an earlier request is ignored
    (this matching was replaced by one connection per request, item 19). A
    delta whose application triggers a rebuild no longer reports `live`. The
    view summary and interval change only when the matching snapshot is
    applied; the transit panel is then marked pending (never zero) until that
    window's counts load, and a transit response for an earlier window is
    dropped. Browser tests hold back the snapshot with `route_web_socket` for
    both paths, each with a positive control once it is released; the test
    helper `set_window` waits for one more snapshot, the live state, the new
    summary and the end of the pending transit state.

19. **Post-merge repair (O65): one connection per request.** O63 matched a
    snapshot to the request by area and interval, so with views A, B, then A
    again, the late answer to the first A matched the last one: the page went
    `live` while the later requests were outstanding, and a delta following
    that answer (one scoped to B, say) was applied to view A. Keeping one
    subscribe in flight did not close it: wire v3 errors carry no request,
    and the previous feed's 503 (change feed unreadable) could be taken for
    the answer in flight, freeing the first A's late answer to pass as the
    last one. Each request (new view, bounded-state rebuild, resync or
    reconnect) now has its own WebSocket connection carrying exactly one
    subscribe, so the connection identifies the request with no wire change.
    A request made while that connection is still opening replaces what it
    will send; after it has sent, a new request closes it and opens another.
    Every event of a replaced connection (open, snapshot, error, delta) is
    ignored. Browser tests: A–B–A with the first A's late answer and a stray
    delta after it; A–B–A with the old feed's 503 before that answer; a
    genuine 413 refusal on the new connection, which reads stale instead of
    waiting, and a held refusal superseded by a newer request, which is then
    sent and applied; the bounded-state rebuild on a new connection, with the
    old connection's late delta ignored. Each has a positive control that is
    live with the latest view's data. Cost: one connection handshake per view
    change, within `api.max_websockets` because the replaced connection is
    closed first.

## Consequences

- Snapshots carry no events yet; the event-claim ledger is Package 4c.
- The standard-library server with threads per connection suits a loopback,
  single-user demo; capacity for the private deployment is measured there.
- The threat model document (brief §5) is still to be written; this package
  implements and tests the slow-client, malformed-message, revoked-session and
  foreign-origin cases it will list.
- A green CI run shows the public code and synthetic demo work. It is not
  evidence of the private installation, gateway or login.
