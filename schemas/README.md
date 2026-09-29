# Wire schemas

`eye-wire.v2.schema.json` is the single authoritative definition of every REST
response and WebSocket message (protocol version `eye.wire/2`). Entry points:
`ServerMessage` (health, error, snapshot, delta, resync_required, transits) and
`ClientMessage` (subscribe, unsubscribe).

`eye-wire.v1.schema.json` is version 1 exactly as merged before Package 3, kept
byte for byte (its SHA-256 is pinned in `tests/test_wire.py`). Nothing serves
or accepts it. Tests use it, with its own corpus in `tests/fixtures/wire-v1`,
to show that version-2 messages are refused by the unchanged version-1
validator. Version 2 adds two shapes that version 1 cannot carry: tracks with
contested positions (`conflicts`, and tracks with no route) and transit counts.
Every other version-1 message is also a valid version-2 message once it is
relabelled.

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
   (`eye.wire/3`); keep the old file unchanged.
2. `.venv/bin/python scripts/gen_wire_types.py`
3. Add valid and invalid corpus cases, then `scripts/verify.sh`.

CI fails if the generated files are stale.
