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

Package 5 status (research slice): no source is added. Baselines and the
backtester read recorded rollups of the synthetic sources only; their fixture
(`tests/fixtures/synthetic/research/`) is invented `synthetic-fixture` data.

Package 6 status: one candidate scientific imagery product, Copernicus
Sentinel-2 Level-2A, is reviewed below as `copernicus-sentinel-2-l2a`. It is a
**candidate and stays disabled**. A download adapter exists and is tested
only on invented archives through an offline transport (ADR 0010). The
repository has no real transport for it, no default command or CI run
contacts a Copernicus host, and no real product has been downloaded.
Approval is the owner's decision.

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
| `copernicus-sentinel-2-l2a` | **Candidate, disabled** (Package 6) | `backend/eye/ingest/copernicus.py` (download rules) and `backend/eye/raster/` (archive checks, scratch); synthetic tests only, no real transport | 2026-10-08 (terms read; see below) |

## Candidate rows

### copernicus-sentinel-2-l2a

*Reviewed 2026-10-08 in a Claude cloud session and repaired the same day
for audit finding O75. Read-only: no account was created, no token
requested and no imagery downloaded. Nothing here is approved, and this
reading of the published terms is not approval to operate or publish
imagery.*

**Why this product.** The brief (§7, Package 6) asks for "one permitted
scientific product" and names none. Sentinel-2 Level-2A (surface
reflectance, 10/20/60 m) is a widely used scientific product with a written
EU licence that covers reproduction, distribution and modification. It ships
as a Sentinel-SAFE archive, which suits Package 6's "known product/date
result" and "malformed archive refusal" checks.

**How the facts were read, and its limits.**

- Pages were read through a fetch tool that returns a summary of each page,
  not the raw page. Text in quotes below is the wording that tool returned.
  The reviewer should check it against the live page before approval.
- Plain `curl` to every provider host was refused by the session's proxy
  (HTTP 403 on CONNECT). The fetch tool reached the pages listed below.
- The fetch tool did not return the "Product Download" section of the
  published OData page. That section was read from the page's source file
  in the CDSE documentation repository (listed below), which the published
  page is built from.
- Anything marked UNVERIFIED was not found on a page that was read. A failed
  or silent read is not evidence that a term is absent (CLAUDE.md).

Pages read on 2026-10-08:

| Page | URL | Result |
|---|---|---|
| Sentinel data legal notice (official) | https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice | read; no date shown |
| CDSE terms and conditions | https://dataspace.copernicus.eu/terms-and-conditions | read; no revision date shown |
| CDSE quotas | https://documentation.dataspace.copernicus.eu/Quotas.html | read; no revision date shown |
| CDSE OData API | https://documentation.dataspace.copernicus.eu/APIs/OData.html | read; "Product Download" section not returned |
| CDSE OData API, page source | https://raw.githubusercontent.com/eu-cdse/documentation/main/APIs/OData.qmd | read; "Product Download" section |
| CDSE STAC API | https://documentation.dataspace.copernicus.eu/APIs/STAC.html | read |
| CDSE access token | https://documentation.dataspace.copernicus.eu/APIs/Token.html | read |
| CDSE Sentinel-2 data page | https://documentation.dataspace.copernicus.eu/Data/SentinelMissions/Sentinel2.html | read |
| SentiWiki S2 products | https://sentiwiki.copernicus.eu/web/s2-products | read |
| Copernicus Sentinel data licence (rev. 1), ECMWF copy | https://ecds.ecmwf.int/licences/ec-sentinel | read; secondary copy, superseded here by the official notice |

**Which terms apply to what.** CDSE terms §3 split two kinds of content:

- **Sentinel data.** Access "is available on a free, full and open basis"
  and its access and use are governed by the *Legal notice on the use of
  Copernicus Sentinel Data and Service Information*. The licence fields
  below come from that notice.
- **Other portal content.** "Any other contents of the Copernicus Data
  Space Ecosystem portal are intended for non-commercial use." The
  no-resale, no-redistribution and no-derivative-works sentence sits in
  this paragraph and applies to this other portal content (information,
  documents, images and material of the web portal), not to Sentinel data.

The fields:

- Status: **candidate, disabled**
- Data class: imagery (satellite surface reflectance, Level-2A)
- Occurrence-time guarantee or reporting-delay watermark: not applicable
  (not an occurrence report). Each product carries its own sensing time.
  Timeliness after sensing: **open, UNVERIFIED** (not stated on the pages
  read).
- Reuse rights per item and deletion or refresh obligations: the legal
  notice applies to all Sentinel data, not per item. No deletion or refresh
  obligation was found on the pages read: **UNVERIFIED**. The notice allows
  specific limitations of access and use "in the rare cases of security
  concerns, protection of third party rights or risk of service
  disruption". Products may be reprocessed under a newer processing
  baseline (the CDSE Sentinel-2 page says historical L1C/L2A up to 13
  December 2023 will be available in baseline 5.0 or better), so a stored
  product needs its baseline recorded and a re-fetch path.
- API endpoint(s):
  - OData catalogue search:
    `https://catalogue.dataspace.copernicus.eu/odata/v1/Products`, filtered
    on `productType` `S2MSI2A`. Whether search needs a token is
    **UNVERIFIED** (the page's examples send none).
  - OData product download: `odata/v1/Products(<Id>)/$value` with the
    header `Authorization: Bearer <access token>`. The page source says
    "only authorized users are allowed to download data products". Its
    curl and wget examples use host `catalogue.dataspace.copernicus.eu`;
    its Python example uses `download.dataspace.copernicus.eu`. A `$zip`
    path for compressed native-format download is documented for
    Sentinel-1 only.
  - STAC catalogue: base `https://stac.dataspace.copernicus.eu/v1/`. The
    published STAC page says the legacy endpoint
    `https://catalogue.dataspace.copernicus.eu/stac` is deprecated from
    17 November 2025. The CDSE Sentinel-2 page links the STAC collection
    `sentinel-2-l2a`. Whether STAC search or asset access needs a token:
    **UNVERIFIED**.
  - S3 access: the Sentinel-2 page gives no S3 path for L2A. **UNVERIFIED**.
- Official terms URL and review date: the official legal notice and the
  CDSE terms in the table above, read 2026-10-08.
- Permitted purpose: the legal notice grants free access for
  reproduction; distribution; communication to the public; adaptation,
  modification and combination with other data and information; and any
  combination of these, "in so far as it is lawful". It draws no
  commercial/noncommercial line. Access is without any express or implied
  warranty, and users waive claims for damages against the EU and the data
  providers. The non-commercial limit in CDSE terms §3 applies to other
  portal content, not Sentinel data.
- Rate limits and credit/cost calculation: no fee is stated (free, full and
  open). Free-tier quotas for S3, OData and STAC on immediately available
  data: 4 concurrent connections; 2000 requests per minute (stated for S3
  only); 20 MB/s per connection; 12 TB per rolling 30 days, after which
  bandwidth drops to 1 MB/s and 1 connection. CDSE terms §9 forbid bypassing
  limits with multiple accounts and allow "immediate cessation or limitation
  of the services" on breach.
- Worst-case requests and cost per day at configured bounds: the adapter
  caps downloads started per UTC day (`imagery.max_products_per_day`,
  default 4) and bytes received per UTC day (`imagery.max_bytes_per_day`,
  default 6 GB, never exceeded: no read asks for more than the day has
  left). Each download makes at most `max_redirects` + 1 requests
  (default 4), so the default worst case is 16 download requests and 6 GB
  per day, or 180 GB per 30 days against the 12 TB quota. The byte defaults
  are provisional: the size of one L2A product is still **UNVERIFIED** and
  must be measured before enabling. Monetary cost: none stated.
- Storage rights (raw / derived / retention limit): the legal notice grants
  reproduction and modification of Sentinel data, so storing raw products
  and derived tiles is within its terms. No retention limit was found:
  **UNVERIFIED**. Storage is bounded by EYE's own quota and the raster
  worker's scratch budget.
- Redistribution rights: the legal notice permits distribution and
  communication to the public of Sentinel data, with the attribution below.
  The CDSE no-redistribution sentence covers other portal content only.
  Whether EYE publishes any Sentinel imagery or derived raster (public
  repo, CI or private install) is **the owner's open imagery decision**;
  until it is made, no real imagery, tile cache or derived raster goes in
  the public repo or CI.
- Required attribution text and placement: unmodified data, "Copernicus
  Sentinel data [Year]"; modified data (any resampled, reprojected, tiled or
  composited output), "Contains modified Copernicus Sentinel data [Year]".
  The notice requires informing recipients of the source when distributing
  or communicating to the public; placement is not specified, so EYE would
  show it in the layer legend and the evidence panel of every view that
  renders the layer.
- Credential handling: download requires a registered CDSE account (terms
  §4). Token endpoint
  `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token`,
  `password` grant, client id `cdse-public`; with two-factor authentication
  a `totp` value is added. The quotas page says an access token "stays
  active for 10 minutes" and can be refreshed with the refresh token
  "anytime within 60 minutes after the access token is generated"; after
  that it must be re-generated. The password grant means the ingest service
  would hold the account password at runtime; that is a private server
  runtime file, never git, CI or a cloud session, and owned by ingest only,
  not the raster worker.
- Egress destination(s): `catalogue.dataspace.copernicus.eu`,
  `download.dataspace.copernicus.eu`, `stac.dataspace.copernicus.eu` and
  `identity.dataspace.copernicus.eu` (documented). Redirect destinations
  of a download: **open, UNVERIFIED**.
- Failure and outage behaviour: the adapter refuses with a stable reason
  code. HTTP 401 and 403 give `provider_auth_refused`; HTTP 429, 5xx and a
  failed connection give `provider_unavailable`; a broken transfer gives
  `transfer_failed`. The partial file is always deleted. The notice allows
  access limits for "risk of service disruption", and quota breach can cut
  service, so the layer must report coverage as unknown or stale, never as
  clear sky or no change. No layer or coverage record exists yet (later
  slice).
- Redirect policy: the real redirects are still **UNVERIFIED**. The
  documented curl examples pass `--location-trusted`, which follows
  redirects and resends the bearer token, but no page names where a
  download redirects. The adapter sends the token only to
  `download.dataspace.copernicus.eu` (exact network location, `https`, no
  port or user information). A redirect may go to another path on that
  host, at most `imagery.max_redirects` times. Any other destination is
  refused before a request is made, including the catalogue, STAC and
  identity hosts. A real redirect off the download host therefore fails
  closed until it is measured and reviewed.
- Test fixture: invented SAFE-shaped archives built in each test's
  temporary directory by `tests/support/synthetic_safe.py`. Band files are
  text placeholders, not imagery, and nothing is committed.
- Disable switch: `imagery.enabled` must be false (true is refused), the
  real transport always refuses (`source_disabled`), and `providers.enabled`
  still refuses every non-synthetic source (`backend/eye/config.py`).
- Approved by / date: **not approved**.

**Other facts read (product).** 110 × 110 km tiles in UTM/WGS84; L2A at
10 m, 20 m or 60 m; revisit 5 days with two satellites (2 to 3 days at
mid-latitudes); SAFE format with JPEG2000 images; L2A pilot products from
28 March 2017, operational from mid-March 2018 (Euro-Mediterranean) and
global from 13 December 2018.

**Still open before real use.** The adapter is built and disabled. These
remain open:

1. The owner's imagery decision: whether EYE uses or publishes Sentinel
   imagery at all, and where (private install, public repo or neither).
2. Whether a CDSE account (password grant) is acceptable for the private
   server.
3. A measured L2A product size, download redirect destinations and
   timeliness after sensing.
4. Operating caps: provisional defaults are set in `[imagery]` (ADR 0010);
   the owner confirms them once a product size is measured.
5. A private download transport and token handling, reviewed in their own
   change. The AWS COG copy is a separate, unapproved alternative with its
   own hosts and format.

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
