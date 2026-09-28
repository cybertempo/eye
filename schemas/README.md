# Wire schemas

`eye-wire.v1.schema.json` is the single authoritative definition of every REST
response and WebSocket message (protocol version `eye.wire/1`). Entry points:
`ServerMessage` (health, error, snapshot, delta, resync_required) and
`ClientMessage` (subscribe, unsubscribe).

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
  `additionalProperties: false`, `enum`, `const`, `items`, `minItems`, `maxItems`,
  `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `oneOf`, plus
  `title`/`description`. Anything else is refused when the schema loads.
- Every object is closed and every array and string is bounded.
- Coverage in state `unknown`/`failed` must carry `"value": null` and a reason;
  a number there is invalid. Observed and received times are separate required
  fields.

## Changing the schema

Run on the developer laptop, then commit all three:

1. Edit the schema (a breaking change needs a new file and version, `eye.wire/2`).
2. `.venv/bin/python scripts/gen_wire_types.py`
3. Add valid and invalid corpus cases, then `scripts/verify.sh`.

CI fails if the generated files are stale.
