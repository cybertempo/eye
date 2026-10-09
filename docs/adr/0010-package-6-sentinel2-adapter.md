# ADR 0010: Package 6 Sentinel-2 L2A adapter (synthetic only, disabled)

Date: 2026-10-09. Status: proposed (awaiting owner review).

Scope:
- **Built:** a download adapter for one bounded product, Copernicus Sentinel-2
  Level-2A (`copernicus-sentinel-2-l2a`). It covers destination and redirect
  rules, streamed size caps, archive and metadata checks, product-date
  handling, attribution, a bounded scratch cache with eviction, and an
  `[imagery]` configuration table. Synthetic SAFE-shaped archives and an
  offline transport drive 142 tests.
- **Not built:** any real network transport, token request or credential;
  catalogue or STAC search; JPEG2000 decoding, tiling or any derived raster;
  a database ledger for imagery; wire or browser changes. The wire schema
  stays `eye.wire/4` and no migration is added.
- **Data:** invented archives only, built in each test's temporary directory.
  No real product, imagery, provider account or private path is used or
  committed.
- **No AI:** no model is called or consulted anywhere in this path.

## Decisions

1. **The source stays disabled by construction.** The repository has no real
   transport for this source. `transport_from_config` always refuses with
   `source_disabled`, `imagery.enabled = true` is refused by the configuration
   loader, and `providers.enabled` still refuses every non-synthetic source.
   A test checks that the adapter modules import no network library. Enabling
   a real download needs the owner's imagery decision, an approved register
   row and a private transport, reviewed in their own change.

2. **Ingest holds the token; the raster worker does not.** Downloading is
   `backend/eye/ingest/copernicus.py`. It takes the bearer token from a
   caller-supplied function on each request, so a refreshed token is used.
   Archive checks and the scratch cache are `backend/eye/raster/` and take no
   token. The token never appears in an error message or in `record.json`.

3. **Exact destination, checked before every request.** A URL is allowed only
   when all of these hold:
   - the scheme is `https`;
   - the network location is exactly `download.dataspace.copernicus.eu` (no
     port, user name, password, other case or trailing dot);
   - it has no fragment, and no whitespace, control character or backslash.

   The first request is always `/odata/v1/Products(<id>)/$value` for a
   lower-case UUID. A redirect (301, 302, 303, 307, 308) may go to another
   path on the same host; it needs a `Location`, cannot revisit a URL, and at
   most `max_redirects` are followed. An unlisted destination is refused
   before any request, so it receives nothing, the token included. Other
   documented CDSE hosts (catalogue, STAC, identity) are refused too, because
   this adapter only downloads. Where CDSE really redirects a download is
   still UNVERIFIED (register row), so a redirect off the download host fails
   closed until a measured redirect is recorded and reviewed.

4. **Size is capped while streaming.** A `Content-Length` above the cap is
   refused before the body is read. The body is read in `chunk_bytes` pieces
   and stopped as soon as it passes the smaller of `max_product_bytes` and
   the bytes left in today's budget. A body longer or shorter than its
   declared length is refused. Every byte received counts against the daily
   budget, whether the product is admitted or not. A refusal deletes the
   partial file.

5. **A malformed archive is refused, never repaired.** Each check has a
   stable reason code. The archive must be a zip whose members all sit under
   the requested product's `.SAFE` directory. These are refused:
   - an absolute path, `..`, backslash or duplicate name;
   - a symbolic link, an encrypted member, or compression other than stored
     or deflated;
   - more than `max_archive_members` members, more than
     `max_uncompressed_bytes` in total, or a member that expands more than
     `max_compression_ratio` times;
   - a bad CRC (every member is streamed once);
   - a missing `manifest.safe` or `MTD_MSIL2A.xml`, one larger than
     `max_metadata_bytes`, or XML with a DOCTYPE or ENTITY declaration;
   - any manifest-listed file that is missing or fails its MD5 or SHA3-256.

6. **Dates come from the product, never from EYE's clock.** These must hold:
   - the name's sensing time equals `DATATAKE_SENSING_START` to the second;
   - `PRODUCT_START_TIME` is not after `PRODUCT_STOP_TIME`;
   - `GENERATION_TIME` is not before `PRODUCT_STOP_TIME`;
   - the discriminator is not before the sensing time;
   - no sensing time precedes 28 March 2017 (Level-2A availability, register
     row);
   - nothing is more than five minutes in the future.

   Times must be ISO 8601 UTC with `Z`, and an impossible calendar date is
   refused. Product type, level, baseline, mission and `PRODUCT_URI` must
   match the requested name.

7. **Freshness and attribution.** Age is measured from sensing time. A
   product older than `stale_after_hours` is `stale`, and a view must show
   its sensing date rather than present it as current. Attribution follows the
   legal notice: "Copernicus Sentinel data [Year]" for the archive, and
   "Contains modified Copernicus Sentinel data [Year]" for any resampled,
   reprojected, tiled or composited output. The year is the sensing year in
   UTC.

8. **Bounded scratch with checked eviction.** The cache holds only
   re-fetchable provider archives, never EYE capture history. A download
   first reserves `max_product_bytes`. It evicts least recently used products
   only when that makes room within `max_scratch_bytes` and
   `max_cached_products`. If evicting everything would not be enough, nothing
   is evicted and the request is refused. Daily caps on products started and
   bytes received reset at UTC midnight. One process holds the scratch root
   (file lock). A partial file left by a stopped process is deleted on open.
   A damaged `record.json` or `budget.json` stops the store rather than being
   guessed at.

## Limits and their defaults

All limits are in `[imagery]` and are explicit before any real product is
enabled. The byte defaults are provisional, because no real L2A product size
has been measured.

| Key | Default | Meaning |
|---|---|---|
| `max_product_bytes` | 1,500,000,000 | one archive, declared or streamed |
| `max_scratch_bytes` | 4,000,000,000 | all cached archives plus the download in progress |
| `max_cached_products` | 2 | archives kept on scratch |
| `max_products_per_day` | 4 | downloads started per UTC day |
| `max_bytes_per_day` | 6,000,000,000 | bytes received per UTC day; the daily worst case |
| `max_redirects` | 3 | redirects per download, on the download host only |
| `max_archive_members` | 1000 | zip members |
| `max_uncompressed_bytes` | 2,000,000,000 | total expanded size |
| `max_compression_ratio` | 100 | per member |
| `max_metadata_bytes` | 4,000,000 | each of `manifest.safe` and `MTD_MSIL2A.xml` |
| `chunk_bytes` | 1,048,576 | read size; memory does not grow with product size |
| `stale_after_hours` | 240 | after this, the product is shown as stale |

The loader refuses `max_scratch_bytes` or `max_bytes_per_day` below
`max_product_bytes`. CDSE's published free quota is 12 TB per rolling 30 days
(register row). The default daily worst case of 6 GB is 180 GB over 30 days,
far below that quota.

## Consequences

- The structure checks follow the published SAFE layout. Whether a real CDSE
  product passes them is UNVERIFIED until one is measured privately. A
  mismatch fails closed and needs a reviewed change.
- The AWS Sentinel-2 L2A COG copy (registry.opendata.aws/sentinel-2-l2a-cogs,
  no account) is an unapproved alternative. It would be a different source
  with its own register row, hosts and format (COG, not SAFE). It is not
  built here.
- Before a real product is enabled, these remain open: the owner's imagery
  decision, a CDSE account decision, a measured product size, redirects and
  timeliness, and a private transport. A database ledger and coverage
  reporting for the layer are a later slice.
