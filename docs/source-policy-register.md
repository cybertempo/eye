# Data-source policy register

One row per source (brief §6). **A source with any blank, unknown or unapproved
field stays disabled.** The configuration loader refuses every entry in
`providers.enabled` until an adapter exists and its row here is approved.

Package 2 status: **no real source is approved and no real provider adapter
exists.** The only data sources are the synthetic fixture sets in
`tests/fixtures/synthetic/`. The real AIS region (San Francisco Bay / Golden
Gate) is decided, but its NOAA/MarineCadastre historical subset and any live
AIS source still need their own rows here before any adapter is enabled.

Package 4c status: the event-claim ledger is source-independent. **No aviation,
marine or road occurrence-report source is approved and no adapter for one
exists.** Each needs its own row (brief §6: occurrence reports are separate
from position feeds), recording whether it is automatic, licensed or
manual-document entry, before an adapter is enabled.

## Register

| Source id | Status | Adapter | Reviewed |
|---|---|---|---|
| `synthetic-fixture` | Approved for demo and tests | built-in fixture files; `backend/eye/ingest/capture.py` | 2026-09-28 |
| `synthetic-ais` | Approved for demo and tests | invented AIS reports from `scripts/gen_ais_fixtures.py`; `backend/eye/ingest/synthetic_ais.py` (only `SYNV-` vessel ids) | 2026-09-28 |
| `synthetic-events` | Approved for demo and tests | invented event-report claims in `tests/fixtures/synthetic/events/` (format `eye.synthetic-event-claims/1`); parsed by the source-independent `backend/eye/ingest/event_claims.py`. Case ids, evidence references (`synthetic-doc:…`) and summaries are invented; no real authority, aircraft, vessel or road is described | 2026-09-29 |

## Row template

Copy this block for each candidate source. Record facts from the provider's
official pages, with the date you read them. Do not paste credentials, account
identifiers or private hostnames.

```markdown
### <source id>

- Status: candidate | approved | disabled | rejected
- Data class: position feed | occurrence report | news metadata | imagery | ephemeris | other
- API endpoint(s): <public documentation URL, not a keyed URL>
- Official terms URL and review date:
- Permitted purpose (research / noncommercial / commercial):
- Rate limits and credit/cost calculation:
- Worst-case requests and cost per day at configured bounds:
- Storage rights (raw / derived / retention limit):
- Redistribution rights (public repo / private install / link only):
- Required attribution text and placement:
- Credential handling (where the secret lives at runtime; never in git):
- Egress destination(s) (hostnames the adapter may contact):
- Failure and outage behaviour (coverage state reported):
- Redirect policy:
- Test fixture (synthetic or recorded, with its own rights note):
- Disable switch (config key and tested refusal):
- Approved by / date:
```
