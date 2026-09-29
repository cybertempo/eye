// Measured facts as text. Pure functions of validated wire messages: nothing
// here depends on the globe, its preset or the screen, so the same message
// always gives the same facts whatever the rendering settings are.

import type {
  Coverage,
  EventCase,
  EventClaim,
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
 * Event-report coverage for the view, per layer, in words; unknown is never
 * "none". Every row is labelled as a reporting window; a row without that
 * label is refused rather than read as an occurrence interval.
 */
export function eventCoverageText(coverage: readonly Coverage[], layers: readonly string[]): string[] {
  const lines = layers.map((layer) => {
    const rows = coverage
      .filter((c) => c.layer === layer && c.metric.name === "event_cases_in_view")
      // A total order, so a live page and a fresh REST view list rows alike.
      .sort((a, b) =>
        compareTime(a.interval.start, b.interval.start) ||
        compareTime(a.interval.end, b.interval.end) ||
        JSON.stringify(a).localeCompare(JSON.stringify(b)));
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
  return [...lines, EVENT_COUNT_NOTE];
}
