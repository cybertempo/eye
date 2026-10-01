# Wire schemas

`eye-wire.v4.schema.json` is the single authoritative definition of every REST
response and WebSocket message (protocol version `eye.wire/4`). Entry points:
`ServerMessage` (health, error, snapshot, delta, resync_required, transits) and
`ClientMessage` (subscribe, unsubscribe).

Earlier versions are kept byte for byte as merged, each with the corpus it was
merged with; their SHA-256 values are pinned in `tests/test_wire.py`. Nothing
serves or accepts them. Tests show the unchanged earlier validators refuse
every current message, and the new shapes even when relabelled:

- `eye-wire.v1.schema.json` (`tests/fixtures/wire-v1`): as merged before
  Package 3. Version 2 added tracks with contested positions (`conflicts`,
  tracks with no route) and transit counts.
- `eye-wire.v2.schema.json` (`tests/fixtures/wire-v2`): as merged with Package
  3. Version 3 replaced the placeholder `Event` (never sent) with event-ledger
  cases (`EventCase`, below).
- `eye-wire.v3.schema.json` (`tests/fixtures/wire-v3`): as merged with Package
  4c. Version 4 added news and media items (`media`, `media_suggestions`,
  `media_upserted`) and the `news` coverage layer (below).

Every other earlier message is still a current message once relabelled, with
the empty `media` arrays version 4 requires added.

- **Server side:** `backend/eye/wire/validate.py` validates every outgoing body
  and any incoming client message at runtime.
- **Browser side:** `scripts/gen_wire_types.py` generates
  `web/src/generated/wire-types.ts` (types) and `wire-schema.ts` (embedded
  schema); `web/src/wire-validate.ts` validates every incoming message before use.
- **Cross-check:** `tests/test_wire.py` runs the Python validator, the compiled
  browser validator and the reference `jsonschema` library over
  `tests/fixtures/wire/{valid,invalid}`; all three must agree.

## Rules

- Keyword subset only: `$defs`, `$ref` (local), `type`, `properties`, `required`,
  `additionalProperties: false`, `enum`, `const`, `items` (a schema or `false`),
  `prefixItems` (tuples such as `[longitude, latitude]`), `format: "date-time"`
  (a real calendar date: leap years, month lengths, years 0001-9999), `minItems`, `maxItems`,
  `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `oneOf`, plus
  `title`/`description`. Anything else is refused when the schema loads.
- Every object is closed and every array and string is bounded.
- Positions are `[longitude, latitude]` and bounding boxes are
  `[west, south, east, north]`; latitudes outside -90..90 are refused.
- The reference library treats `format` as an annotation, so corpus cases that
  test calendar dates carry a `reference_divergence` note; Python's `datetime`
  and the browser's own leap-year rule are cross-checked instead.
- Timestamps carry 0 to 6 fractional digits, so a client must compare them as
  times, not strings ("03:04:00Z" sorts after "03:04:00.5Z" as text). The
  browser uses `compareTime` in `web/src/facts.ts`.
- Coverage in state `unknown`/`failed` must carry `"value": null` and a reason;
  a number there is invalid. Observed and received times are separate required
  fields.
- A track is one `(source, layer, record id)`; its id is `trk-` plus 128 bits of
  SHA-256 over `[source, layer, record]`. `points` is the route and holds
  resolved positions only. An observed time whose latest publications conflict
  appears under `conflicts` with every claim and its evidence, never in the
  route. A track has at least one point or one conflict (`TrackRouted` or
  `TrackUnresolved`). A conflict has 2 to 20 claims; the server refuses a
  request (413, naming the record and time) rather than send more or fewer.
  A client breaks the drawn route at every contested time that falls between
  two resolved points.
- An event case (`EventCase`) is one source's case with every version as
  `claims`, oldest first. A claim is either a `ReportClaim` (official or
  operator basis, a report kind and status, a required `evidence_ref`) or a
  `CandidateClaim` (basis `motion_inference`, a motion kind such as
  `signal_lost`, `ais_gap`, `vessel_stopped` or `traffic_slowdown`, status
  `candidate`, no evidence reference); a candidate can never carry an
  accident, casualty or collision kind. `reported_event_location` is a point,
  a segment with its affected `direction`, or a closed area ring, each with
  `precision_m`. `last_observed_position` is separate, comes from a track, and
  is present only when `link.state` is `linked`. `standing` and
  `current_claim_id` follow the current claim; conflicting latest claims have
  `is_current: null` and the case is `unresolved`. The browser refuses a case
  whose standing, current claim or link disagree.
- Event-report coverage is served with metric `event_cases_in_view` and
  `interval_kind: "reporting_window"`: the row's interval is its capture's
  reporting window (when the reports were published, not when events
  happened), unclipped, and a row from a capture names it in `batch_id`.
  Its value counts the cases in the view's own event list whose current
  report came from that capture, so a later report about an earlier event
  is counted in the later window, and the same claim delivered by two
  captures is counted by both rows: rows are never a total. The stored
  per-batch `event_reports` metric counts the whole capture and is never
  served as a view count. A part
  of the view's interval that no event source covered over the whole view
  area is sent as `unknown` with a reason, never omitted, so absent reports
  are never read as "no events". A source whose area covers only part of the
  view is sent as `partial` (a lower bound), never as a measured zero. When
  new coverage changes these gaps, the server resnapshots.
- A reporting window measures reports published in it, never which events
  occurred. Every snapshot therefore carries, per requested layer, an
  `interval_kind: "occurrence_window"` row (metric
  `event_occurrence_completeness`) for the view's interval with state
  `unknown` and its reason. No approved source gives an occurrence-time
  guarantee or reporting-delay watermark, and the browser refuses any such
  row claiming `qualified` or `partial`.
- A case is in a requested interval when its nominal event time ± stated
  uncertainty overlaps it; the nominal time and uncertainty are served as
  stated.
- A news or media item (`MediaItem`, `eye.wire/4`) is one article, image or
  video with every version as `versions`, kept apart from event cases.
  - Each `MediaVersion` carries the source's `first_published_time` and
    `revision_time`, EYE's `received_time`, the delivering
    `evidence_batch_ids`, an https `url` (and `syndicated_from`), publisher,
    creator, `headline` (untrusted text), `language`, `rights`, a
    `capture_time_claimed` for images and video, and an optional `place`
    with `role`, `method` and `precision_m`.
  - `rights` is `licensed` (with `licence` and `attribution`), `link_only` or
    `unknown`. An unknown-rights version has `headline: null` and
    `headline_withheld: true` (EYE never stores its headline), and the
    browser refuses a version where `headline_withheld` does not match
    unknown rights, or that carries a headline with them.
  - `media_suggestions` pairs are `syndicated_copy` or `place_and_time`,
    always `status: suggestion`: never a merge or a confirmation.
  - News coverage rows use layer `news` and metric `media_items_in_view`,
    as reporting windows. `news` is not a subscription layer.
- Transit counts (`transits`) have three forms:
  `qualified` (exact numbers, every uncertainty count 0, no reason), `partial`
  (numbers are lower bounds, reason required) and `unknown`/`failed` (null
  numbers, reason required). A crossing's time is `estimated_time` with its
  method; there is no observed-time field for it. The browser also refuses a
  count whose total is not inbound + outbound, which JSON Schema cannot express.

## Version mismatch

A message's `schema_version` is read before it is validated, so another
version is reported as such rather than as a malformed message. The server
answers a client message in another version with a 400 error naming the
version it speaks, then closes the socket with 1003. The browser, on a server
message in another version, stops live updates without resubscribing or
reconnecting, keeps the data shown under the stale banner, and asks for a
reload.

## Changing the schema

Run on the developer laptop, then commit all three:

1. Edit the schema. A change that an existing validator would refuse, or that
   gives an existing field a new meaning, needs a new file and version
   (`eye.wire/5`); keep the old file unchanged, pin its hash in
   `tests/test_wire.py` and copy its corpus to `tests/fixtures/wire-vN`.
2. `.venv/bin/python scripts/gen_wire_types.py`
3. Add valid and invalid corpus cases, then `scripts/verify.sh`.

CI fails if the generated files are stale.
