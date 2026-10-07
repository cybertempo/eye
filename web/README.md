# Web client

THEATRE and DESK (Package 3). TypeScript compiled by `tsc` into ES modules;
no bundler, framework, map library, token or external service.

| Module | Role |
|---|---|
| `src/app.ts` | page wiring: default view over REST, live feed, tabs, keyboard controls, view form |
| `src/live.ts` | WebSocket client: one connection and one subscribe per request, snapshot, sequenced deltas, gap detection, reconnect with a resume cursor |
| `src/globe.ts` | THEATRE: orthographic globe on a 2D canvas (graticule only, no basemap); rendering presets |
| `src/desk.ts` | DESK: observed transits, cited crossings and coverage; world events; news and media evidence; fact tables |
| `src/facts.ts` | measured facts as text; pure functions, independent of rendering |
| `src/wire-validate.ts` | runtime validation against the wire schema |
| `src/generated/` | types and embedded schema generated from `schemas/eye-wire.v4.schema.json` |

Every server message is validated before use; values are inserted with
`textContent` only. An unavailable or invalid response is shown as unknown,
never as zero. The page loads only same-origin scripts and styles (the server's
Content-Security-Policy refuses anything else). News headlines and media
metadata are untrusted text; a link to an original is an ordinary `https`
link a person may follow, never fetched, embedded or opened by the page.

Build (developer laptop, CI): `scripts/setup.sh` runs `npm ci` and `npm run build`.
The compiled `dist/` directory is not committed. Browser tests
(`tests/test_browser.py`) drive it headless through Playwright; no window opens.
