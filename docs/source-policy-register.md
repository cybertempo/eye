# Data-source policy register

One row per source (brief §6). **A source with any blank, unknown or unapproved
field stays disabled.** The configuration loader refuses every entry in
`providers.enabled` until an adapter exists and its row here is approved.

Package 0 status: **no source is approved and no provider adapter exists.**
The only data source is the synthetic fixture set in `tests/fixtures/synthetic/`.

## Register

| Source id | Status | Adapter | Reviewed |
|---|---|---|---|
| `synthetic-fixture` | Approved for demo and tests | built-in fixture file | 2026-09-28 |

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
