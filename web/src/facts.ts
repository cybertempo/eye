// Measured facts as text. Pure functions of validated wire messages: nothing
// here depends on the globe, its preset or the screen, so the same message
// always gives the same facts whatever the rendering settings are.

import type {
  Coverage,
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
