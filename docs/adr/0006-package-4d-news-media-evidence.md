# ADR 0006: Package 4d news and media evidence (provider-neutral part)

Date: 2026-09-30. Status: proposed (awaiting owner review).

Scope:
- **Built:** a source shortlist, a provider-neutral ingestion and display path
  for news and media items, and synthetic fixtures.
- **Not built:** no news, video or image provider adapter is built or
  enabled. Each waits for its own approved source-policy row (brief §6).
- **Shortlist limits:** see
  [`docs/news-media-source-shortlist.md`](../news-media-source-shortlist.md).
  This session could not read any provider's official page (its network
  policy blocked them), so every fact field there is UNVERIFIED.

## Decisions

1. **Media items are their own evidence, apart from event claims.** The brief
   (§4, "Near-real-time news and media") proposed ingesting news metadata as
   discovery candidates into the event-claim ledger. The owner's Package 4d
   instruction keeps them separate, and this package follows it.
   - **Own tables:** items live in their own tables (migration 0006).
   - **No references:** no media table refers to an event table, or the
     other way round (tested).
   - **No events:** an item never creates, confirms or links an event case.
   - **Later work:** feeding an event review queue from news would be a
     later, separately reviewed step.
2. **One row per item version, metadata and a link only.** A media item is
   one version of one article, image or video, as its source published it:
   - **What it holds:** kind, status, first-publication and revision times,
     the original URL (https only, no credentials), an optional syndication
     original, publisher, creator, headline, language, reuse rights, a
     claimed capture time for images and video, and an optional place.
   - **Validated in full before archiving:** raw evidence is kept exactly
     and can never be deleted. So a news capture is checked completely
     before anything is archived, and refused whole on any failure:
     - **Duplicate keys:** a key repeated in any one JSON object, at any
       depth, refuses the capture. A parser keeps the last value, so a
       repeat would let validated text differ from the archived bytes,
       which keep the first. This applies to every capture format.
     - **Envelope:** the capture's own keys and those of `request`,
       `attempt` and `provider_response` must be on reviewed allowlists.
     - **Adapter text:** the adapter's `note` and `attempt.written_by` are
       bounded safe text.
     - **Items:** every item must pass the full strict parse, which checks
       key names, value types and lengths, nested `place` and `rights`
       shapes, times and the reporting window. A body, image bytes, a
       thumbnail or any unreviewed field fails it.
     - **Failed attempts:** a failed or indeterminate attempt carries no
       items.
     - **No per-item rejections:** a news capture never has them. A future
       adapter must clean its capture before building it.
     - *Review repairs:* the first version rejected bad items only after
       archiving. The second checked only top-level item key names, so
       over-long values, nested keys, bare-string items, malformed rights
       and envelope text still reached `eye.raw_evidence`. The third
       accepted duplicate JSON keys whose first value carried text. A test
       covers each of those routes.
   - **Ids and receipts:** the id is derived from source, item id and
     content, so an exact repeat delivery adds a receipt
     (`eye.media_item_receipt`, receipt time stamped by EYE and checked
     against the batch), not an item.
   - **Append-only:** items and receipts refuse UPDATE and DELETE.
3. **Versions by the source's revision time.** `eye.media_item_version`
   ranks an item's versions by revision time, never by load order.
   - Updates, corrections and retractions are later versions, and every
     version stays.
   - Two versions with the same revision time and different content
     conflict: `is_current` is NULL and the item is `unresolved`.
   - A news capture is a reporting window: an item revised outside the
     capture's requested interval is rejected from that capture.
4. **Untrusted text, bounded and never interpreted.** Headlines, publishers,
   creators and URLs are bounded on ingest, in the database and in the wire
   schema.
   - **Refused characters:** control characters and bidirectional-override
     characters are refused on ingest.
   - **Stored as given:** markup is stored exactly and rendered only with
     `textContent`. A browser test checks that an `onerror` payload does not
     run.
   - **No AI:** no model reads anything on the way in.
5. **Rights per item.**
   - **`licensed`:** needs a licence identifier and the attribution text, and
     only a licensed item has them.
   - **`link_only`:** shows metadata and a link.
   - **`unknown`:** a link only. Its headline is never stored:
     - **Capture:** a capture carrying one is refused before archiving (the
       adapter drops it).
     - **Database:** the constraint `unknown_rights_store_no_headline`
       refuses one.
     - **API:** it serves `headline: null` with `headline_withheld: true`.
     - **Browser:** it refuses a version that breaks either rule.
     - *Review repair:* the first version hid the headline only in the API
       while storing it.
   - **No copies:** EYE never stores or processes the work itself, whatever
     the rights.
6. **Places say what they are.** A place has a role (`event_place`,
   `publisher_location` or `mentioned`), a method (`source_stated` or
   `automated_geocode`) and a precision of at least 1 m.
   - **What THEATRE draws:** only a source-stated event place, as a dotted
     approximate area at its stated precision, never as a point.
   - **What it never draws:** a publisher's city (the wrong-city case), a
     mentioned place, an automated geocode or a retracted item.
   - **Images and video:** drawn only when the creator's claimed capture time
     lies inside the view. Old footage reposted later is never a current
     mark, and nothing is ever labelled live.
7. **Matching across sources is a suggestion only.** Snapshots carry
   deterministic `media_suggestions` pairs, each with `status: suggestion`.
   - **`syndicated_copy`:** one item names the other as its original, or both
     name the same original. They are not independent confirmation.
   - **`place_and_time`:** different publishers, both with a source-stated
     event place, within their combined stated precision, first published
     within six hours.
   - **What is never used:** automated geocodes and publisher locations.
     Headlines are never compared.
   - **No merging:** suggestions never merge items and never confirm
     anything. DESK words each one as "Suggestion, not confirmed".
   - **Limit:** a snapshot carries at most 500 suggestions. A view with more
     is refused by name (413, with the qualifying count) over REST and the
     WebSocket, never served with a shortened list that would read as if
     the rest did not qualify. *Review repair O64:* the first version cut
     the list to 500 without saying so.
8. **Sources are approved or refused, by name.**
   - **Accepted:** only `synthetic-news` (invented, `.invalid` URLs).
   - **Candidates refused:** the shortlisted providers (`gdelt`,
     `guardian-open-platform`, `nytimes`, `newsapi`, `youtube-data`,
     `wikimedia-commons`, `flickr`, `google-news`) are named in code as
     candidates. They are refused before anything is archived, with a message
     pointing to the shortlist.
   - **Unknown sources refused:** any other source is refused as unapproved.
9. **Coverage.** A news capture's coverage is a reporting window, like event
   reports (ADR 0005, decisions 14 and 16).
   - **Served metric:** each row is served as `media_items_in_view`, with
     `interval_kind: reporting_window` and the capture's `batch_id`.
   - **Outcomes:** a healthy empty capture is a real zero of items published
     then. An outage is `failed`. Time with no capture is `unknown` ("items
     unknown, not absent").
   - **DESK wording:** it says a zero of items does not mean nothing
     happened.
   - **Stored metric:** the stored per-batch metric `media_items` is never
     shown as a view count.
10. **Wire `eye.wire/4`.** Adding `media`, `media_suggestions`,
    `media_upserted` and the `news` coverage layer changes shapes that v3
    refuses, so this is a new version (ADR 0004's rule).
    - **Frozen:** `eye-wire.v3.schema.json` is kept byte for byte, with its
      hash pinned, and its corpus is copied to `tests/fixtures/wire-v3`.
    - **Tested:** v1–v3 validators refuse the new shapes even when
      relabelled, and the current validator refuses every earlier message.
    - **Not a subscription layer:** `news` cannot be subscribed to. Items
      come with every snapshot, bounded by `max_media` (default 200, at most
      500) with the limit refused by name (413).
11. **Live updates.** A news batch of new items that match nothing is an
    ordinary delta (`media_upserted` and its coverage row). A snapshot is
    needed instead (`resync_required`, overflow) when a batch:
    - touches an item another batch delivered (a repeat or a correction);
    - moves an item out of view;
    - takes part in a suggestion;
    - changes the unknown gaps.
    In each case the live page equals a reload (tested).

## Consequences

- **Deletion:** real providers can require deletion (a removal request, or a
  refresh-or-delete period). The append-only ledger has no deletion path yet,
  so any source with such terms needs one before it can be approved (see the
  shortlist's open questions).
- **Crowd sizes:** gathering footprints and crowd-size ranges (brief §4) are
  not built. No count field exists, and a headline's number is only
  untrusted text.
- **Scene reconstruction:** 3D scenes from media, camera pose and integrity
  checks are not built.
- **Reports and events:** there is no link between news items and event
  cases, not even as a suggestion. That would be a separate review.
