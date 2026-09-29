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
   source covered over the whole view area in each requested layer (see
   decision 12), so the absence of a source is never shown as "no events". DESK words it that way and distinguishes a
   measured zero of reports. A measured zero of reports is never a claim that
   no event occurred (decision 16).
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

12. **Review repairs before merge (O56 to O58).**
    - *O56, a source area smaller than the view certifies nothing beyond
      it:* an event-report coverage row closes an unknown gap only when its
      source's requested area covers the whole view. A row whose area only
      overlaps the view is sent as `partial` (its count a lower bound) with
      the reason "the source's area covers only part of this view; outside it
      events are unknown", and the view stays `unknown` for that time. A view
      wholly inside a healthy source's area with nothing reported is a
      measured zero (`qualified`, 0). This rule applies to event-report
      coverage; track coverage rows are listed per batch as before.
    - *O57, cases are selected by when they may have happened:* a case is in
      the requested interval when its possible occurrence interval (nominal
      event time ± stated uncertainty) overlaps it, in snapshots and in live
      deltas alike; a claim with no event time falls back to its publication
      time. The nominal time and uncertainty are served unchanged.
    - *O58, live coverage narrows the unknown gaps:* a client holds the gap
      rows it was sent, and a delta can only add rows. When an event batch's
      coverage changes the gaps in a subscription's view, the server sends
      `resync_required` (`overflow`) with the unchanged cursor and a fresh
      snapshot, so the old gap is narrowed or removed; a batch that changes
      no gap (for example one whose area lies outside the view) is an
      ordinary delta. The live DESK then matches a fresh REST view.

13. **Review repair before merge (O59): counts are for the view.** The stored
    per-batch metric `event_reports` counts the distinct cases in a whole
    capture (its requested area and hour) and is kept unchanged for replay.
    It is never served as a count for a view. Each event-coverage row is
    served as `event_cases_in_view`: its interval is clipped to the view's
    interval, and its value is the number of cases in the view's own event
    list (same area, occurrence-interval and version rules) whose current or
    conflicting latest report came from that batch. DESK reads "covered (n
    cases in this view)". Because a new version of a case can change which
    batch's row counts it, an event batch touching a case that another
    in-view batch's row counts resnapshots rather than sending a delta; a
    batch adding only new cases is still a delta. Rows are listed in a total
    order so a live page and a fresh REST view read alike. (Decision 14
    replaces the clipping.)

14. **Review repair before merge (O60): each row is a reporting window.** An
    event capture asks a source what it published during the request, so a
    row's interval is its capture's *reporting window*, not an occurrence
    interval. Every `event_cases_in_view` row says so on the wire with
    `interval_kind: "reporting_window"`. A row from a capture also carries
    that capture's `batch_id`, which is the id claims cite in
    `evidence_batch_ids`. Two captures with the same window are two rows.
    - **No clipping:** the interval is served unclipped, because its count
      can include a report published anywhere in that window.
    - **Later reports:** a capture whose window lies outside the view's
      interval is still listed when it holds the current report of a case in
      view. A later report or correction about an earlier event is counted
      in the later window, beside the earlier capture's row.
    - **Not a total:** the same claim delivered by two captures is counted
      by both rows, so the rows are not added together.
    - **Gaps:** unknown gaps are still computed from the parts of those
      windows inside the view.
    - **DESK wording:** each row reads "reports published *start* to *end*
      covered (n cases in this view) by capture *id*". A note under the rows
      says the rows are reporting windows. It also says a case can be
      counted in more than one row, that the rows must not be added
      together, and that the event table lists each case once.
    - **Refusal:** DESK refuses an event count that arrives without the
      reporting-window label rather than read it as an occurrence interval.
    - **Live updates:** an event batch touching a case that any other batch
      delivered resnapshots. This covers a repeat, a correction or a later
      report. A batch adding only new cases, even a later report about an
      earlier event, is a delta that carries its own row.
    - **History unchanged:** the stored `event_reports` metric, migrations
      and replay history are unchanged.
15. **Review repair before merge (O61): one labelled display for event
    coverage.** The general coverage table (`#coverage-facts`, under
    "Measured facts") has an unqualified "Interval (UTC)" column and no room
    for the reporting-window label, capture id or non-additive note. So it
    no longer lists event-report rows: a row is an event row when its metric
    is `event_cases_in_view` or it carries `interval_kind` or `batch_id`.
    - **Pointer:** the table's caption points readers to DESK, world events,
      Event-report coverage, where those rows keep their labels.
    - **Empty table:** a view with no track coverage says so and gives the
      same pointer.
    - **Unchanged:** track coverage rows, their order and their wording; the
      wire and the API.

16. **Review repair before merge (O62): reports are not occurrences.** A
    reporting window measures which reports a source published in it, and
    an empty capture is a real zero of those reports. It cannot certify
    which events *occurred* then, because a source can publish its first
    report of a 12:30 event after 13:00.
    - **Occurrence rows:** every snapshot adds, per requested layer, a row
      with `interval_kind: "occurrence_window"`, metric
      `event_occurrence_completeness`, the view's interval and state
      `unknown`. Its reason: no approved event source gives an
      occurrence-time guarantee or reporting-delay watermark.
    - **Reporting windows unchanged:** reporting windows still close
      reporting gaps.
    - **DESK wording:**
      - Each layer's occurrence line reads "events that occurred *start*
        to *end*: completeness Unknown: …".
      - With every reporting window captured and no case, the table says
        the captures found no reports in their windows, and that whether
        any event occurred is Unknown.
      - Otherwise it says reports are unknown for part of the view.
    - **Refusal:** DESK refuses any occurrence row that claims `qualified`
      or `partial`.
    - **No guarantee path yet:** none exists in code. An approved source
      would need a register row recording its guarantee with evidence, and
      a wire field carrying that evidence, before anything could claim
      completeness.
    - **Live updates:** occurrence rows are constant for a subscription, so
      a first late report stays an ordinary delta.
    - **Unchanged:** the stored metric, raw evidence and replay.

## Consequences

- No worker derives review candidates from tracks yet; candidates arrive as
  claims. A later worker must write them through the same ledger and rules.
- Cross-source deduplication (two sources reporting one event) is not done:
  each source's case is shown on its own. News deduplication is Package 4d.
- Links use only EYE track identifiers; a registry for registrations, MMSI or
  IMO numbers needs its own source review.
- Real occurrence-report adapters, their coverage areas and their
  redistribution rights are later work, one source at a time.
