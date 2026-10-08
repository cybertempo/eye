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

Package 4d status: the news and media path is provider-neutral. **No news,
image or video source is approved and no adapter for one exists.** Candidates
are listed, with their official pages and every fact field still UNVERIFIED,
in [`news-media-source-shortlist.md`](news-media-source-shortlist.md). The
capture pipeline names each candidate and refuses it until its row here is
approved. A news row must also record reuse rights per item (licence and
attribution, link-only, or unknown), whether items carry source-stated or
automated locations, and any deletion or refresh obligation. The append-only
ledger has no deletion path yet (ADR 0006).

Package 5 status (lifecycle slice): no source is added. Retention prunes only
raw evidence bytes of synthetic sources after a verified synthetic backup;
ledger rows are never deleted. Provider deletion or refresh obligations (for
example for a video or photo service) are **not** encoded anywhere and stay
UNVERIFIED until a source's official terms are reviewed in its own row.

Occurrence completeness: EYE treats each event capture as a reporting window
(which reports were published then), never as proof of which events occurred.
Occurrence completeness is shown as unknown unless a source's approved row
records an explicit, evidenced occurrence-time guarantee or reporting-delay
watermark, and a wire field carries it. `synthetic-events` gives none, and
no code path claims completeness yet.

## Register

| Source id | Status | Adapter | Reviewed |
|---|---|---|---|
| `synthetic-fixture` | Approved for demo and tests | built-in fixture files; `backend/eye/ingest/capture.py` | 2026-09-28 |
| `synthetic-ais` | Approved for demo and tests | invented AIS reports from `scripts/gen_ais_fixtures.py`; `backend/eye/ingest/synthetic_ais.py` (only `SYNV-` vessel ids) | 2026-09-28 |
| `synthetic-news` | Approved for demo and tests | invented news and media items in `tests/fixtures/synthetic/media/` (format `eye.synthetic-media-items/1`); parsed by the source-independent `backend/eye/ingest/media_items.py`. Publishers, headlines, people and links are invented; every URL uses the reserved `.invalid` domain; no body, image or video is stored | 2026-09-30 |
| `synthetic-events` | Approved for demo and tests | invented event-report claims in `tests/fixtures/synthetic/events/` (format `eye.synthetic-event-claims/1`); parsed by the source-independent `backend/eye/ingest/event_claims.py`. Case ids, evidence references (`synthetic-doc:…`) and summaries are invented; no real authority, aircraft, vessel or road is described | 2026-09-29 |

## Row template

Copy this block for each candidate source. Record facts from the provider's
official pages, with the date you read them. Do not paste credentials, account
identifiers or private hostnames.

```markdown
### <source id>

- Status: candidate | approved | disabled | rejected
- Data class: position feed | occurrence report | news metadata | imagery | ephemeris | other
- Occurrence-time guarantee or reporting-delay watermark (occurrence reports only; with evidence, or "none"):
- Reuse rights per item (news and media only: licence and attribution, link only, or unknown) and deletion or refresh obligations:
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
