# News and media source shortlist (Package 4d)

*Prepared 2026-09-29 in a Claude cloud session. Nothing here is approved.
Every candidate below stays disabled: no adapter exists, no default command
or CI run calls one, and `backend/eye/ingest/capture.py` refuses every
source except the synthetic ones.*

## How this list was made, and its limits

The session's network policy **blocked every provider page** it tried to
read: gdeltproject.org, open-platform.theguardian.com, developers.google.com
and newsapi.org. Web search was allowed, so each official URL below was
located by search, but the pages themselves were **not read**.

For that reason every fact field is marked **UNVERIFIED**. A failed read is
not evidence of what a page says (CLAUDE.md: a failed instrument is
`UNVERIFIED`).

Where a search result carried a snippet, it is quoted under *search lead*.
A lead is only a pointer for the reviewer. It must be checked against the
live official page before it is recorded as a fact.

**Who does what.** The owner, or a session whose network policy allows
these hosts, reads each official page. They record the facts in a
`docs/source-policy-register.md` row, and only then decides approval.

The fields each row must settle, for every source:

- **Access:** access method and credential handling.
- **Cost:** cost, quota and rate limit, with the worst case per day at
  EYE's configured bounds.
- **Coverage:** geographic and time coverage (history depth).
- **Delay:** update delay (publication to availability).
- **Attribution:** the required attribution text and where it goes.
- **Reuse:** reuse rights, split into storage (raw, metadata, media),
  display, redistribution and derived use.
- **Retention:** retention or refresh obligations.
- **Corrections:** how corrections, retractions and removals are signalled,
  and whether EYE must delete on request.
- **Location:** whether locations are source-stated or automated, and how
  accurate they are.

## Candidates

| Candidate | Data class | Official pages located | Status |
|---|---|---|---|
| GDELT 2.0 (events, mentions, GKG files; DOC 2.0 and GEO 2.0 APIs) | news metadata (automated extraction) | [about and terms of use](https://www.gdeltproject.org/about.html#termsofuse), [data](https://www.gdeltproject.org/data.html), [DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/), [GEO 2.0 API](https://blog.gdeltproject.org/gdelt-geo-2-0-api-debuts/), [event codebook](http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf) | candidate, disabled |
| The Guardian Open Platform | publisher API | [open-platform.theguardian.com](https://open-platform.theguardian.com/) (blocked; the terms and access pages were not located) | candidate, disabled |
| New York Times Developer APIs | publisher API | [developer.nytimes.com](https://developer.nytimes.com/) (search found no terms page) | candidate, disabled |
| NewsAPI.org | commercial aggregator | [pricing](https://newsapi.org/pricing), [terms](https://newsapi.org/terms), [docs](https://newsapi.org/docs) | candidate, disabled |
| YouTube Data API | video metadata | [developer policies](https://developers.google.com/youtube/terms/developer-policies), [API services terms](https://developers.google.com/youtube/terms/api-services-terms-of-service), [storage policy](https://developers.google.com/youtube/terms/derived-metrics-policy) | candidate, disabled; link and embed only |
| Wikimedia Commons (MediaWiki API) | licensed media | [reusing content](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia), [licensing](https://commons.wikimedia.org/wiki/Commons:Licensing), [credit line](https://commons.wikimedia.org/wiki/Commons:Credit_line) | candidate, disabled |
| Flickr API | licensed or unlicensed photos | [API terms of use](https://www.flickr.com/help/terms/api), [API guide](https://www.flickr.com/services/developer/api/) | candidate, disabled |
| Google News | none | no documented API; the old [News Search API](https://developers.google.com/news-search/) is deprecated | **not usable**: the brief forbids building on an undocumented RSS endpoint |

### GDELT 2.0

- **Access:** raw CSV files, DOC/GEO APIs and BigQuery. UNVERIFIED.
- **Cost and quota:** UNVERIFIED.
  - *Search lead:* "available for unlimited and unrestricted use for any academic, commercial, or governmental use … without fee".
- **Attribution:** UNVERIFIED.
  - *Search lead:* any use or redistribution "must include a citation to the GDELT Project and a link" to its website.
- **Reuse:** UNVERIFIED.
  - *Search lead:* "redistribute, rehost, republish, and mirror".
  - Whether that covers the linked articles themselves (third-party publishers) is **not** established. EYE would store metadata and links only.
- **Update delay:** 15-minute batches (brief §4). UNVERIFIED here.
- **Coverage:**
  - *Search lead:* DOC 2.0 searches "a rolling window of the last 3 months".
  - 65 machine-translated languages. UNVERIFIED.
- **Location:** automated geocoding. The brief cites GDELT's own warning that locations can be wrong.
  - An adapter must mark every GDELT location `automated_geocode`.
  - Such a location is never an event place without another source.
- **Corrections:** UNVERIFIED. GDELT re-extracts; publishers' corrections reach it only if re-crawled.

### The Guardian Open Platform

Every field is UNVERIFIED: the site was blocked for both reading and search.
- **To confirm:** key tiers, rate limits, whether a non-commercial key allows this use, whether body text may be stored, attribution, and removal obligations.

### New York Times Developer APIs

Every field is UNVERIFIED: search returned no pages.
- **To confirm:** API terms, rate limits, storage limits on returned metadata, attribution, and whether display outside the NYT site is permitted.

### NewsAPI.org

- **Cost and plan:** UNVERIFIED.
  - *Search lead:* the free Developer plan "may be used for development and testing in a development environment only, and cannot be used in a staging or production environment (including internally)".
  - A private production installation would therefore need a paid plan, if the lead is accurate.
- **Everything else:** delay, history depth, attribution, storage and corrections are UNVERIFIED.

### YouTube Data API

- **Storage:** UNVERIFIED.
  - *Search lead:* API clients "must not download, import, backup, cache, or store copies of YouTube audiovisual content without YouTube's prior written approval".
  - *Search lead:* non-authorized data may be stored "not longer than 30 calendar days", then deleted or refreshed.
  - This matches the brief's citation of the developer policy.
- **EYE design consequence:** link or embed only. Metadata must carry a refresh-or-delete deadline before any adapter exists.
- **Quota, attribution and corrections:** UNVERIFIED.

### Wikimedia Commons

- **Rights:** a licence per file. UNVERIFIED.
  - *Search lead:* files are "usually CC BY, CC BY-SA, or GFDL" or public domain, with the licence stated "on its file description page".
  - *Search lead:* `API:Imageinfo` with `iiprop=extmetadata` returns credit and attribution fields.
- **EYE design consequence:** rights are per item. An item without a machine-readable licence must stay `unknown`.
- **Capture time and location:** uploader-supplied, so they are claims, not verified facts.

### Flickr API

- **Rights:** UNVERIFIED.
  - *Search lead:* photos are "owned by the users"; commercial use needs a Creative Commons licence permitting it.
  - *Search lead:* removal "within 24 hours" when an owner asks.
  - *Search lead:* "under 3600 queries per hour across the whole key".
- **EYE design consequence:** rights are per photo, and the default is `unknown`. Removal must reach stored metadata within the stated bound, which needs a delete path that the append-only design does not yet have (see ADR 0006).

## What the synthetic path already enforces for any future adapter

- **Approved sources only:** a capture from a source without an approved row is refused before anything is archived. The candidates above are named in code as known and unapproved, so their refusal is tested.
- **Metadata and links only:** EYE stores metadata and a link, never article bodies, images or video. A capture whose items carry any field outside the reviewed allowlist is refused before anything is archived, because raw evidence is permanent. An adapter must drop such fields before building the capture.
- **Rights per item:** a source's policy row allows metadata storage, and each item states its own reuse rights. An unknown-rights item is stored as a link without its headline; a capture carrying one is refused before archiving.
- **Untrusted text:** headlines and media metadata are untrusted text. They are bounded, stored as given, and rendered only as text.
- **Location roles:** a location says what it is (the event place, the publisher's location, or a place merely mentioned) and how it was obtained (stated by the source, or an automated geocode).

## Open questions for the owner

1. Which candidate to review first. GDELT is free, if the lead holds, but its geocoding is automated. A publisher API gives edited metadata but has narrower coverage.
2. **Takedowns:** whether a source's deletion obligations (for example YouTube's 30-day refresh, or Flickr's 24-hour removal) are acceptable. Each needs a deletion path for stored metadata that an append-only ledger does not have yet.
3. Whether a private installation may store headlines at all for sources whose terms allow links only.
