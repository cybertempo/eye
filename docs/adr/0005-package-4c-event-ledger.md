# ADR 0005: Package 4c event-claim ledger (source-independent part)

Date: 2026-09-29. Status: proposed (awaiting owner review).

Scope: the shared ledger, its API and browser presentation, and synthetic
fixtures. No aviation, marine or road occurrence-report adapter is built or
enabled; each waits for its own source-policy row (brief §6).

## Decisions

1. **Append-only claims (migration 0005).** A claim is one version of one
   source's case, exactly as published: source, layer, case id, basis, kind,
   status, event time with its uncertainty, publication time, reported
   geometry (point, road segment with its affected direction, or closed area
   ring) with stated precision, evidence reference, subject identifiers and
   time window, and summary. Its id is derived from source, layer, case id and
   content, so a duplicate delivery adds a receipt (`eye.event_claim_receipt`,
   receipt time stamped by EYE and checked against the batch) and never a
   second claim. Claims and receipts refuse UPDATE and DELETE.
2. **Corrections, retractions and conflicts by publication time.**
   `eye.event_claim_version` ranks a case's claims by the source's publication
   time, never by load order, exactly as observations are ranked. A later
   claim supersedes; a retraction is a later claim with status `retracted`.
   Claims published at the same time with different content are a conflict:
   `is_current` is NULL and the case is `unresolved` until a later claim
   settles it. Every version stays in the ledger and in every message.
3. **Motion data proves nothing.** A claim whose basis is `motion_inference`
   (lost flight signal, AIS gap, stopped vessel, traffic slowdown) can only be
   a review candidate: status `candidate`, a candidate kind, no evidence
   reference. A candidate kind can only come from motion inference, and a
   sourced report (official or operator) must cite evidence. Three separate
   mechanisms refuse anything else: the capture parser (the record is rejected
   and counted, coverage becomes partial), a database constraint
   (`motion_inference_is_only_a_candidate`) and the wire schema
   (`ReportClaim` versus `CandidateClaim`). The browser words a candidate as
   "Review candidate … Not a report of any accident; outcome unknown." Only a
   sourced report's current version can say "accident", "casualty" or
   "collision", and only a `final` one says "confirmed".
4. **Reported location and last observed position are separate.** The ledger
   stores only the reported event location. The last observed position is read
   from observations when a case is served, only for a linked track, and is
   the track's last resolved position in the subject window, no later than
   the event time plus its uncertainty. The wire keeps them in separate fields,
   DESK in separate columns, and THEATRE draws them as separate marks never
   joined by a line. A point is drawn no sharper than its stated precision.
5. **Conservative links.** A case links to a track only when its current
   claim names `track:source:layer:record` identifiers, gives a time window,
   exactly one named track of the case's layer has a resolved observation in
   that window, and that observation lies within the claim's precision plus
   20 km of the reported location. Otherwise the case stays unlinked, with
   the reason: no subject named (`not_applicable`), no observed match, more
   than one match (`ambiguous`, with the candidate track ids), too far, or
   conflicting latest claims. Other identifier schemes (registration,
   callsign, MMSI, IMO, plate) are kept and shown but not matched: no approved
   registry exists to verify them.
6. **Unknown coverage.** An event capture batch records coverage with metric
   `event_reports` (the number of distinct cases; failed or unknown with a
   null value on a provider failure). A snapshot adds an `unknown` coverage
   row, with its reason, for every part of the view's interval that no event
   source covered in each requested layer, so the absence of a source is
   never shown as "no events". DESK words it that way and distinguishes a
   measured zero ("No event cases reported where event-report sources covered
   this view").
7. **Wire version `eye.wire/3`.** Serving event cases changes a shape that
   `eye.wire/2` defined (the placeholder `Event`, never sent), which the v2
   validator would refuse, so this is a new version, following the rule set in
   ADR 0004 (O53). `eye-wire.v2.schema.json` stays byte for byte as merged,
   with its corpus in `tests/fixtures/wire-v2`; the unchanged v1 and v2
   validators refuse every v3 message and the new event shapes even when
   relabelled, and every other earlier message relabelled is still valid v3.
   The browser refuses a case whose standing, current claim or link
   disagree (rules the schema cannot express).
8. **Deltas.** An event batch sends the cases it changed that have, or had,
   a version in the subscription. A correction that moves a case out of view
   cannot be an upsert, so the server sends `resync_required` (`overflow`)
   with the unchanged cursor and a fresh snapshot, as for tracks (O48). A
   position batch also resends every case that names one of its tracks, so a
   link and last observed position follow new positions.
9. **Replay.** `verify_replay` re-derives every claim, receipt and version
   from stored evidence and reports anything stored that no evidence
   produces. Loading the event fixtures in either order gives the same ids.
10. **Bounds.** At most 1,000 claims per capture, 100 versions per case and
    100 evidence batches per claim (refused by name past these, never cut
    short), `api.max_events` cases per snapshot (default 500), and at most 10
    subject identifiers.
11. **Demo.** Six invented batches (`tests/fixtures/synthetic/events/demo`)
    cover 12:00 to 14:00 of the demo day: a marine casualty whose area is
    revised and which links to the demo vessel `SYNV-0020`, an AIS-gap
    candidate for `SYNV-0021`, an aviation accident whose site is revised in a
    final report, a signal-loss candidate, road congestion, a slowdown
    candidate, and a collision on a directional segment that is then cleared.
    14:00 to 16:00 has no event source and shows as unknown.

## Consequences

- No worker derives review candidates from tracks yet; candidates arrive as
  claims. A later worker must write them through the same ledger and rules.
- Cross-source deduplication (two sources reporting one event) is not done:
  each source's case is shown on its own. News deduplication is Package 4d.
- Links use only EYE track identifiers; a registry for registrations, MMSI or
  IMO numbers needs its own source review.
- Real occurrence-report adapters, their coverage areas and their
  redistribution rights are later work, one source at a time.
