// Measured facts as text. Pure functions of validated wire messages: nothing
// here depends on the globe, its preset or the screen, so the same message
// always gives the same facts whatever the rendering settings are.

import type {
  Coverage,
  EventCase,
  EventClaim,
  MediaItem,
  MediaSuggestion,
  MediaVersion,
  Track,
  TrackPoint,
  TransitCount,
  TransitCrossing,
  TransitsMessage,
} from "./generated/wire-types.js";

export const UNKNOWN = "Unknown";

const SOURCE_LABELS: Record<string, string> = {
  "synthetic-ais": "Synthetic AIS (invented vessels; not a real AIS feed)",
  "synthetic-fixture": "Synthetic fixture (invented records)",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? `Source ${source}`;
}

export interface CountFigures {
  total: string;
  inbound: string;
  outbound: string;
  qualifier: string;
}

/** A missing count is text, never 0; a partial count is a visible lower bound. */
export function countFigures(count: TransitCount): CountFigures {
  switch (count.state) {
    case "qualified":
      return {
        total: String(count.total),
        inbound: String(count.inbound),
        outbound: String(count.outbound),
        qualifier: "Qualified: exact count",
      };
    case "partial":
      return {
        total: `at least ${count.total}`,
        inbound: `at least ${count.inbound}`,
        outbound: `at least ${count.outbound}`,
        qualifier: "Partial: lower bound",
      };
    default:
      return {
        total: `${UNKNOWN} (no count)`,
        inbound: UNKNOWN,
        outbound: UNKNOWN,
        qualifier: count.state === "failed" ? "Failed: capture failed, no count" : "Unknown: no usable count",
      };
  }
}

export function uncertaintyText(count: TransitCount): string {
  return (
    `ambiguous ${count.ambiguous_crossings}; across boundary ${count.boundary_crossings}; ` +
    `insufficient-evidence gaps ${count.insufficient_gaps}`
  );
}

/**
 * A wire timestamp as a key that sorts chronologically. Wire times are UTC with
 * a Z suffix and fixed-width date and time fields, but 0 to 6 fractional
 * digits, so plain string order is wrong: "03:04:00Z" sorts after
 * "03:04:00.5Z" because "Z" follows ".". Padding the fraction to six digits
 * keeps microsecond precision (no float or Date rounding) and makes string
 * order chronological.
 */
export function timeKey(time: string): string {
  const dot = time.indexOf(".");
  const seconds = dot === -1 ? time.slice(0, -1) : time.slice(0, dot);
  const fraction = dot === -1 ? "" : time.slice(dot + 1, -1);
  return `${seconds}.${fraction.padEnd(6, "0")}`;
}

/** Negative, zero or positive as ``a`` is before, at or after ``b``. */
export function compareTime(a: string, b: string): number {
  const left = timeKey(a);
  const right = timeKey(b);
  return left < right ? -1 : left > right ? 1 : 0;
}

export function intervalText(interval: { start: string; end: string }): string {
  return `${interval.start} to ${interval.end}`;
}

/** Rules the schema cannot express; a message that breaks one is refused. */
export function transitsErrors(message: TransitsMessage): string[] {
  const errors: string[] = [];
  const crossings = new Set(message.crossings.map((c) => c.id));
  const coverage = new Set(message.coverage.map((c) => c.id));
  for (const count of message.counts) {
    if (count.state === "qualified" || count.state === "partial") {
      if (count.total !== count.inbound + count.outbound) {
        errors.push(`count ${count.count_id}: total is not inbound + outbound`);
      }
    }
    if (compareTime(count.interval.end, count.interval.start) <= 0) errors.push(`count ${count.count_id}: empty interval`);
    for (const id of count.crossing_ids) {
      if (!crossings.has(id)) errors.push(`count ${count.count_id}: cites missing crossing ${id}`);
    }
    for (const id of count.coverage_ids) {
      if (!coverage.has(id)) errors.push(`count ${count.count_id}: cites missing coverage ${id}`);
    }
  }
  return errors;
}

export function coverageMetric(coverage: Coverage): string {
  const value = coverage.metric.value;
  return value === null ? `${coverage.metric.name}: ${UNKNOWN} (no data)` : `${coverage.metric.name}: ${value}`;
}

export function crossingTime(crossing: TransitCrossing): string {
  return `${crossing.estimated_time} (estimated by linear interpolation)`;
}

export function formatPosition(lon: number, lat: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(5)}°${ns}, ${Math.abs(lon).toFixed(5)}°${ew}`;
}

export interface TrackFacts {
  id: string;
  record: string;
  kind: string;
  source: string;
  displayType: string;
  points: string;
  lastObserved: string;
  lastReceived: string;
  lastPosition: string;
  claims: string[];
  flags: string;
}

export function trackFacts(track: Track): TrackFacts {
  // The last position is the latest resolved point, unless a later observed
  // time is contested: then the track has no known last position, and no
  // claim is picked over another.
  // Times are compared chronologically (compareTime), never as strings.
  const latest = <T extends { observed_time: string }>(items: readonly T[]): T | undefined =>
    items.reduce<T | undefined>(
      (best, item) => (best === undefined || compareTime(item.observed_time, best.observed_time) > 0 ? item : best),
      undefined,
    );
  const last = latest(track.points);
  const conflicts = track.conflicts ?? [];
  const latestConflict = latest(conflicts);
  const contestedLast =
    latestConflict !== undefined && (!last || compareTime(latestConflict.observed_time, last.observed_time) > 0);
  const claims = conflicts.flatMap((conflict) =>
    conflict.claims.map((c) =>
      `${conflict.observed_time}: ${formatPosition(c.lon, c.lat)} (observation ${c.observation_id}, ` +
        `published ${c.published_time}, received ${c.received_time}, batches ${c.evidence_batch_ids.join(", ")})`,
    ),
  );
  return {
    id: track.id,
    record: track.source_record_id,
    kind: track.kind,
    source: sourceLabel(track.source),
    displayType: track.display_type,
    points: String(track.points.length),
    lastObserved: contestedLast ? latestConflict.observed_time : last?.observed_time ?? UNKNOWN,
    lastReceived: contestedLast ? "Contested: see claims" : last?.received_time ?? UNKNOWN,
    lastPosition: contestedLast
      ? `Unresolved: ${latestConflict.claims.length} conflicting claims at ${latestConflict.observed_time}`
      : last ? formatPosition(last.lon, last.lat) : UNKNOWN,
    claims,
    flags: track.quality_flags.length > 0 ? track.quality_flags.join(", ") : "none",
  };
}

/**
 * The route as runs of resolved points. A contested observed time between two
 * resolved points breaks the route there: joining the points either side would
 * draw a path through a time whose position is unknown. Pure and independent
 * of rendering, so every preset breaks the route at the same places.
 */
export function routeRuns(track: Track): TrackPoint[][] {
  const breaks = (track.conflicts ?? []).map((c) => c.observed_time).sort(compareTime);
  const points = [...track.points].sort((a, b) => compareTime(a.observed_time, b.observed_time));
  const runs: TrackPoint[][] = [];
  let run: TrackPoint[] = [];
  let next = 0; // first break not yet passed
  for (const point of points) {
    let broken = false;
    while (next < breaks.length && compareTime(breaks[next] ?? "", point.observed_time) < 0) {
      broken = true;
      next += 1;
    }
    if (broken && run.length > 0) {
      runs.push(run);
      run = [];
    }
    run.push(point);
  }
  if (run.length > 0) runs.push(run);
  return runs;
}

// --- event-ledger cases (Package 4c) -----------------------------------------------------

const KIND_LABELS: Record<string, string> = {
  aviation_accident: "Aviation accident",
  aviation_incident: "Aviation incident",
  emergency_declared: "Emergency declared",
  diversion: "Diversion",
  flight_arrival: "Flight arrival",
  signal_lost: "Flight position reports stopped",
  marine_casualty: "Marine casualty",
  vessel_distress: "Vessel distress",
  vessel_port_arrival: "Port arrival",
  ais_gap: "AIS reports stopped",
  vessel_stopped: "Vessel stopped",
  road_collision: "Road collision",
  road_incident: "Road incident",
  road_closure: "Road closure",
  road_congestion: "Road congestion",
  traffic_slowdown: "Traffic slowdown",
};
const BASIS_LABELS: Record<string, string> = {
  official_report: "official report",
  operator_report: "operator report",
  motion_inference: "inferred from motion data",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export function currentClaim(event: EventCase): EventClaim | undefined {
  return event.claims.find((c) => c.claim_id === event.current_claim_id && c.is_current === true);
}

/**
 * What the case says, in words. Only a sourced report can name an accident;
 * a review candidate (motion data) always says it reports nothing.
 */
export function eventAssessment(event: EventCase): string {
  const current = currentClaim(event);
  if (event.standing === "unresolved" || !current) {
    const latest = event.claims.filter((c) => c.is_current === null);
    const when = latest[0]?.published_time ?? "the same time";
    return `Unresolved: ${latest.length} conflicting claims published at ${when}; no current version`;
  }
  const kind = kindLabel(current.kind);
  if (current.basis === "motion_inference") {
    return `Review candidate: ${kind.toLowerCase()} (${BASIS_LABELS[current.basis]}). ` +
      "Not a report of any accident; outcome unknown.";
  }
  const basis = BASIS_LABELS[current.basis] ?? current.basis;
  switch (current.status) {
    case "final":
      return `${kind}: confirmed by a final ${basis}`;
    case "preliminary":
      return `${kind}: preliminary ${basis} (may change)`;
    case "retracted":
      return `${kind}: retracted by the source`;
    case "cleared":
      return `${kind}: cleared (${basis})`;
    default:
      return `${kind} reported (${basis}); not confirmed by a final report`;
  }
}

export function locationText(location: EventClaim["reported_event_location"]): string {
  const precision = `stated precision ±${location.precision_m} m`;
  switch (location.type) {
    case "point":
      return `Point ${formatPosition(location.coords[0], location.coords[1])}, ${precision}`;
    case "segment": {
      const first = location.coords[0];
      const last = location.coords[location.coords.length - 1];
      const direction = location.direction === "forward"
        ? "one direction only (as drawn, first to last)"
        : "both directions";
      return `Segment from ${first ? formatPosition(first[0], first[1]) : "?"} to ` +
        `${last ? formatPosition(last[0], last[1]) : "?"}, ${direction}, ${precision}`;
    }
    default:
      return `Area (ring of ${location.coords.length - 1} corners), ${precision}`;
  }
}

export function eventTimeText(claim: EventClaim): string {
  if (claim.event_time === null) return `${UNKNOWN} (the source gave no event time)`;
  return `${claim.event_time} ± ${claim.event_time_uncertainty_s ?? 0} s`;
}

export function lastObservedText(event: EventCase): string {
  const last = event.last_observed_position;
  if (!last) {
    return event.link.state === "not_applicable"
      ? "None: the report names no tracked aircraft or vessel"
      : `${UNKNOWN}: not linked (${event.link.reason})`;
  }
  return `${formatPosition(last.lon, last.lat)} at ${last.observed_time} (received ${last.received_time}), ` +
    `track ${last.track_id} (${last.source_record_id}). An observation, not the reported site.`;
}

export function claimHistory(event: EventCase): string[] {
  return event.claims.map((c) => {
    const state = c.is_current === true ? "current" : c.is_current === null ? "conflicting" : "superseded";
    return `v${c.version} (${state}), published ${c.published_time}, received ${c.received_time}: ` +
      `${kindLabel(c.kind)}, ${c.status}, ${BASIS_LABELS[c.basis] ?? c.basis}; ` +
      `${locationText(c.reported_event_location)}; evidence ${c.evidence_ref ?? "none (motion data)"}; ` +
      `batches ${c.evidence_batch_ids.join(", ")}`;
  });
}

/** Rules the schema cannot express; a case that breaks one is refused, not shown. */
export function eventErrors(event: EventCase): string[] {
  const errors: string[] = [];
  const current = event.claims.filter((c) => c.is_current === true);
  if (event.current_claim_id === null) {
    if (current.length > 0) errors.push("a current claim without current_claim_id");
    if (event.standing !== "unresolved") errors.push("no current claim but not unresolved");
  } else {
    const chosen = currentClaim(event);
    if (!chosen || current.length !== 1) errors.push("current_claim_id is not the one current claim");
    else {
      const expected = chosen.status === "retracted" ? "retracted"
        : chosen.basis === "motion_inference" ? "review_candidate" : "report";
      if (event.standing !== expected) errors.push(`standing ${event.standing} does not match the current claim`);
    }
  }
  const last = event.last_observed_position;
  if (last && (event.link.state !== "linked" || last.track_id !== event.link.track_id)) {
    errors.push("a last observed position without a link to that track");
  }
  return errors;
}

/** Why per-batch event counts are never a total; shown with the coverage rows. */
export const EVENT_COUNT_NOTE =
  "Each event-report row is one capture's reporting window (when its reports were published), " +
  "not when events happened. A case is counted in the row of every capture that delivered its " +
  "latest report: a repeated delivery counts it in more than one row, and a later report or " +
  "correction about an earlier event is counted in the later window. Do not add the rows together; " +
  "the event table lists each case once.";

/**
 * Occurrence completeness, per layer. A reporting window never certifies
 * which events occurred, and no approved source gives an occurrence-time
 * guarantee or reporting-delay watermark, so the only acceptable state is
 * unknown: any other claim is refused rather than shown.
 */
export function occurrenceText(coverage: readonly Coverage[], layer: string): string {
  const rows = coverage.filter((c) => c.layer === layer && c.interval_kind === "occurrence_window");
  if (rows.length === 0) return `${layer}: events that occurred in this view: completeness ${UNKNOWN}: not stated`;
  const parts = rows
    .sort((a, b) => compareTime(a.interval.start, b.interval.start) || compareTime(a.interval.end, b.interval.end))
    .map((c) => {
      const span = `events that occurred ${intervalText(c.interval)}`;
      if (c.state === "qualified" || c.state === "partial") {
        return `${span}: refused: a completeness claim without an approved occurrence-time guarantee`;
      }
      return `${span}: completeness ${UNKNOWN}: ${c.reason ?? "no guarantee"}`;
    });
  return `${layer}: ${parts.join("; ")}`;
}

/**
 * Event-report coverage for the view, per layer, in words; unknown is never
 * "none". Every row is labelled as a reporting window; a row without that
 * label is refused rather than read as an occurrence interval. A line per
 * layer then states occurrence completeness, which reporting windows never
 * certify.
 */
export function eventCoverageText(coverage: readonly Coverage[], layers: readonly string[]): string[] {
  const lines = layers.map((layer) => {
    const rows = reportRows(coverage, layer);
    if (rows.length === 0) return `${layer}: ${UNKNOWN}: no event-report coverage`;
    const parts = rows.map((c) => {
      if (c.interval_kind !== "reporting_window") {
        return `${intervalText(c.interval)} refused: an event count without its reporting window`;
      }
      const span = `reports published ${intervalText(c.interval)}`;
      // Which capture the row describes; the event table cites the same ids.
      const by = c.batch_id === undefined ? "" : ` by capture ${c.batch_id}`;
      // The count is scoped to this view (its area and current versions),
      // never the whole capture's count; its time is the reporting window.
      const cases = c.metric.value === 1 ? "1 case" : `${c.metric.value} cases`;
      if (c.state === "qualified") return `${span} covered (${cases} in this view)${by}`;
      if (c.state === "partial") {
        return `${span} partly covered (at least ${cases} in this view; ${c.reason ?? ""})${by}`;
      }
      return `${span} ${UNKNOWN}: ${c.reason ?? "no data"}${by}`;
    });
    return `${layer}: ${parts.join("; ")}`;
  });
  return [...lines, ...layers.map((layer) => occurrenceText(coverage, layer)), EVENT_COUNT_NOTE];
}

function reportRows(coverage: readonly Coverage[], layer: string): Coverage[] {
  return coverage
    .filter((c) => c.layer === layer && c.metric.name === "event_cases_in_view")
    // A total order, so a live page and a fresh REST view list rows alike.
    .sort((a, b) =>
      compareTime(a.interval.start, b.interval.start) ||
      compareTime(a.interval.end, b.interval.end) ||
      JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

/**
 * True when every layer's reporting windows were captured over the whole
 * view: the absence of reports is then a measured zero. It never means no
 * event occurred.
 */
export function reportsMeasured(coverage: readonly Coverage[], layers: readonly string[]): boolean {
  return layers.every((layer) => {
    const rows = reportRows(coverage, layer);
    return rows.length > 0 && rows.every((c) => c.interval_kind === "reporting_window" && c.state === "qualified");
  });
}

// --- news and media evidence (Package 4d) -------------------------------------------

/** The current version, or undefined when the latest versions conflict. */
export function currentVersion(item: MediaItem): MediaVersion | undefined {
  return item.versions.find((v) => v.version_id === item.current_version_id);
}

/** The version to describe: the current one, else the first conflicting latest one. */
export function shownVersion(item: MediaItem): MediaVersion | undefined {
  return currentVersion(item) ?? item.versions.find((v) => v.is_current === null);
}

/** Rules the schema cannot express; an item that breaks one is refused, not shown. */
export function mediaErrors(item: MediaItem): string[] {
  const errors: string[] = [];
  const current = item.versions.filter((v) => v.is_current === true);
  if (item.current_version_id === null) {
    if (current.length > 0) errors.push("a current version without current_version_id");
    if (item.standing !== "unresolved") errors.push("no current version but not unresolved");
  } else {
    const chosen = currentVersion(item);
    if (!chosen || current.length !== 1) errors.push("current_version_id is not the one current version");
    else if (item.standing !== chosen.status) errors.push(`standing ${item.standing} does not match the current version`);
  }
  for (const v of item.versions) {
    const unknown = v.rights.status === "unknown";
    // Unknown reuse rights: a link only. A headline alongside them is refused.
    if (unknown && v.headline !== null) errors.push(`version ${v.version} shows a headline with unknown rights`);
    if (v.headline_withheld && !unknown) errors.push(`version ${v.version} withholds a headline it may show`);
    if (!v.url.startsWith("https://")) errors.push(`version ${v.version} links to a non-https address`);
    if (compareTime(v.revision_time, v.first_published_time) < 0) {
      errors.push(`version ${v.version} was revised before it was published`);
    }
  }
  return errors;
}

const KIND_WORDS: Record<MediaItem["kind"], string> = { article: "Article", image: "Image", video: "Video" };
const STANDING_WORDS: Record<MediaItem["standing"], string> = {
  published: "as first published",
  updated: "updated by its publisher",
  corrected: "corrected by its publisher",
  retracted: "retracted by its publisher",
  unresolved: "latest versions conflict; which is current is unknown",
};

/**
 * What the item is evidence of, and no more: that a publisher published it.
 * It never confirms an event, a place, a time of capture or a crowd size.
 */
export function mediaAssessment(item: MediaItem): string {
  const v = shownVersion(item);
  const who = v ? v.publisher : "its publisher";
  const base = `${KIND_WORDS[item.kind]} (${STANDING_WORDS[item.standing]}). Evidence that ${who} published it; ` +
    "not a verified account of what happened.";
  if (item.kind === "article") return base;
  return `${base} It does not show when or where it was captured, or that it is live.`;
}

function span(seconds: number): string {
  const s = Math.abs(seconds);
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 172_800) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  if (s < 63_072_000) return `${Math.round(s / 86_400)} days`;
  return `${(s / 31_557_600).toFixed(1)} years`;
}

function seconds(later: string, earlier: string): number {
  return (Date.parse(later) - Date.parse(earlier)) / 1000;
}

/** Source times, EYE's receipt time and ages, relative to the end of the view. */
export function mediaTimeText(version: MediaVersion, viewEnd: string): string[] {
  const lines = [`First published ${version.first_published_time} (source time)`];
  if (version.revision_time !== version.first_published_time) {
    lines.push(`This version: ${version.status} ${version.revision_time} (source time)`);
  }
  const delay = seconds(version.received_time, version.revision_time);
  lines.push(`Received by EYE ${version.received_time} (${span(delay)} after this version)`);
  const age = seconds(viewEnd, version.first_published_time);
  lines.push(age >= 0
    ? `Age at the end of this view: ${span(age)}`
    : "Published after the end of this view");
  if (version.capture_time_claimed !== null) {
    const before = seconds(version.first_published_time, version.capture_time_claimed);
    lines.push(`Captured ${version.capture_time_claimed} according to the creator (a claim, not verified); ` +
      `${span(before)} before publication. Not live.`);
  }
  return lines;
}

/**
 * Why a version's place is or is not drawn. Only a place the source states
 * as the event's place is drawn, as an approximate area. An image or video
 * is drawn only when its claimed capture time lies inside the view: old
 * footage reposted later is never shown as a current mark.
 */
export function mediaNotDrawnReason(
  kind: MediaItem["kind"], version: MediaVersion, interval: { start: string; end: string },
): string | null {
  const place = version.place;
  if (place === null) return "no place given";
  if (place.role === "publisher_location") return "the publisher's location, not the event's place";
  if (place.role === "mentioned") return "a place mentioned, not the event's place";
  if (place.method === "automated_geocode") return "an automated geocode, which may be wrong";
  if (version.status === "retracted") return "retracted by its publisher";
  if (kind !== "article") {
    const captured = version.capture_time_claimed;
    if (captured === null) return "no capture time given";
    if (compareTime(captured, interval.start) < 0 || compareTime(captured, interval.end) >= 0) {
      return "captured outside this view, according to its creator";
    }
  }
  return null;
}

/** Whether the globe may draw this version's place. */
export function mediaDrawable(
  kind: MediaItem["kind"], version: MediaVersion, interval: { start: string; end: string },
): boolean {
  return mediaNotDrawnReason(kind, version, interval) === null;
}

/** Where the item says it is about, by role and method, and whether it is drawn. */
export function mediaPlaceText(
  kind: MediaItem["kind"], version: MediaVersion, interval: { start: string; end: string },
): string {
  const place = version.place;
  const reason = mediaNotDrawnReason(kind, version, interval);
  const drawn = reason === null
    ? "Drawn as an approximate area, not a site."
    : `Not drawn: ${reason}.`;
  if (place === null) return `No place given. ${drawn}`;
  const where = `${formatPosition(place.coords[0], place.coords[1])} ± ${place.precision_m} m`;
  if (place.role === "publisher_location") return `Publisher's location ${where}. ${drawn}`;
  if (place.role === "mentioned") return `A place mentioned ${where}. ${drawn}`;
  if (place.method === "automated_geocode") return `Event place from an automated geocode ${where}. ${drawn}`;
  return `Event place stated by the source ${where}. ${drawn}`;
}

export function mediaRightsText(version: MediaVersion): string {
  const rights = version.rights;
  const copy = " EYE stores no copy of the work.";
  if (rights.status === "licensed") {
    return `Licensed ${rights.licence}; attribution: ${rights.attribution}.${copy}`;
  }
  if (rights.status === "link_only") return `Link and metadata only; the work may not be reused.${copy}`;
  return `Reuse rights unknown: link only, headline withheld.${copy}`;
}

export function mediaHeadlineText(version: MediaVersion): string {
  if (version.headline_withheld) return "Headline withheld: reuse rights unknown.";
  return version.headline ?? "No headline given.";
}

export function mediaHistory(item: MediaItem): string[] {
  return item.versions.map((v) => {
    const state = v.is_current === true ? "current" : v.is_current === null ? "conflicting, unresolved" : "superseded";
    return `v${v.version} ${v.status} ${v.revision_time}, received ${v.received_time} (${state}; ` +
      `batches ${v.evidence_batch_ids.join(", ")})`;
  });
}

/** A suggestion in words: never a merge, never a confirmation. */
export function suggestionText(suggestion: MediaSuggestion, names: ReadonlyMap<string, string>): string {
  // Named in a stable, readable order; the wire order is by opaque id.
  const [a, b] = suggestion.items.map((id) => names.get(id) ?? id).sort((x, y) => x.localeCompare(y));
  if (suggestion.basis === "syndicated_copy") {
    return `Suggestion, not confirmed: ${a} and ${b} are copies of one report (syndication). ` +
      "They are not independent confirmation.";
  }
  return `Suggestion, not confirmed: ${a} and ${b} come from different publishers, near the same stated ` +
    "place and time. They may describe the same story; they are not merged and prove nothing more.";
}

/** Why a covered news window with no items is not "nothing happened". */
export const MEDIA_COUNT_NOTE =
  "Each news row is one capture's reporting window: items its source published or revised then. " +
  "No items means that source published none there; it does not mean nothing happened. " +
  "Rows can count an item more than once; do not add them together.";

/** News coverage in words; an absent source is Unknown, never "no news". */
export function mediaCoverageText(coverage: readonly Coverage[]): string[] {
  const rows = coverage
    .filter((c) => c.layer === "news" && c.metric.name === "media_items_in_view")
    .sort((a, b) =>
      compareTime(a.interval.start, b.interval.start) ||
      compareTime(a.interval.end, b.interval.end) ||
      JSON.stringify(a).localeCompare(JSON.stringify(b)));
  if (rows.length === 0) return [`news: ${UNKNOWN}: no news coverage`, MEDIA_COUNT_NOTE];
  const parts = rows.map((c) => {
    if (c.interval_kind !== "reporting_window") {
      return `${intervalText(c.interval)} refused: a news count without its reporting window`;
    }
    const window = `items published ${intervalText(c.interval)}`;
    const by = c.batch_id === undefined ? "" : ` by capture ${c.batch_id}`;
    const items = c.metric.value === 1 ? "1 item" : `${c.metric.value} items`;
    if (c.state === "qualified") return `${window} covered (${items} in this view)${by}`;
    if (c.state === "partial") return `${window} partly covered (at least ${items} in this view; ${c.reason ?? ""})${by}`;
    return `${window} ${UNKNOWN}: ${c.reason ?? "no data"}${by}`;
  });
  return [`news: ${parts.join("; ")}`, MEDIA_COUNT_NOTE];
}

/** True when news reporting windows were captured over the whole view. */
export function mediaMeasured(coverage: readonly Coverage[]): boolean {
  const rows = coverage.filter((c) => c.layer === "news" && c.metric.name === "media_items_in_view");
  return rows.length > 0 && rows.every((c) => c.interval_kind === "reporting_window" && c.state === "qualified");
}
