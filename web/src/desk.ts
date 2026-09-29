// DESK: observed transits for one versioned count line, with the coverage,
// uncertainty, timestamps, evidence IDs and source labels behind each count.
// Values are inserted with textContent only, never as HTML.

import type { Coverage, EventCase, Track, TransitsMessage } from "./generated/wire-types.js";
import {
  claimHistory,
  compareTime,
  currentClaim,
  eventAssessment,
  eventCoverageText,
  eventErrors,
  reportsMeasured,
  eventTimeText,
  kindLabel,
  lastObservedText,
  locationText,
  countFigures,
  coverageMetric,
  crossingTime,
  formatPosition,
  intervalText,
  trackFacts,
  uncertaintyText,
} from "./facts.js";

type Cell = string | Node;

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K, text?: string, className?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function list(values: readonly string[]): Node {
  const out = element("ul", undefined, "claims");
  for (const value of values) out.append(element("li", value));
  return out;
}

function ids(values: readonly string[]): Node {
  if (values.length === 0) return document.createTextNode("none");
  const list = element("ul", undefined, "ids");
  for (const value of values) {
    const item = element("li");
    item.append(element("code", value));
    list.append(item);
  }
  return list;
}

export function fillTable(
  table: HTMLTableElement, caption: string, headings: string[], rows: { cells: Cell[]; className?: string }[],
  emptyText = "No rows for this interval.",
): void {
  table.replaceChildren();
  table.createCaption().textContent = caption;
  const head = table.createTHead().insertRow();
  for (const heading of headings) {
    const th = element("th", heading);
    th.scope = "col";
    head.append(th);
  }
  const body = table.createTBody();
  if (rows.length === 0) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = headings.length;
    cell.textContent = emptyText;
  }
  for (const row of rows) {
    const tr = body.insertRow();
    if (row.className) tr.className = row.className;
    row.cells.forEach((value, index) => {
      const cell = index === 0 ? element("th") : tr.insertCell();
      if (index === 0) {
        (cell as HTMLTableCellElement).scope = "row";
        tr.append(cell);
      }
      if (typeof value === "string") cell.textContent = value;
      else cell.append(value);
    });
  }
}

function definitions(target: HTMLElement, pairs: [string, string][]): void {
  const list = element("dl");
  for (const [term, value] of pairs) list.append(element("dt", term), element("dd", value));
  target.replaceChildren(list);
}

export function renderTransits(message: TransitsMessage): void {
  const summary = document.getElementById("transit-summary");
  if (summary) {
    definitions(summary, [
      ["Source", message.source_label],
      ["Data", message.synthetic ? "SYNTHETIC (invented)" : "Not marked synthetic"],
      ["Count line", `${message.line.name} (${message.line.id}, version ${message.line.version})`],
      ["Algorithm", message.algorithm_version],
      ["Derivation run", message.run_id ?? "none yet"],
      ["Derived at (UTC)", message.derived_at ?? "never"],
      ["Interval (UTC)", intervalText(message.interval)],
      ["Response generated (UTC)", message.generated_at],
    ]);
  }
  const counts = document.getElementById("transit-counts");
  if (counts instanceof HTMLTableElement) {
    fillTable(
      counts,
      "Observed transits per interval. Unknown means no usable count, never zero.",
      ["Interval (UTC)", "State", "Total", "Inbound", "Outbound", "Uncertain items", "Reason", "Coverage cited", "Crossings cited"],
      message.counts.map((count) => {
        const figures = countFigures(count);
        return {
          className: `state-${count.state}`,
          cells: [
            intervalText(count.interval),
            figures.qualifier,
            figures.total,
            figures.inbound,
            figures.outbound,
            uncertaintyText(count),
            count.reason ?? "none",
            ids(count.coverage_ids),
            ids(count.crossing_ids),
          ],
        };
      }),
    );
  }
  const crossings = document.getElementById("transit-crossings");
  if (crossings instanceof HTMLTableElement) {
    fillTable(
      crossings,
      "Crossings cited by these counts, with the observations that bracket them.",
      ["Crossing", "Vessel", "Direction", "Status", "Time", "Window (observed, UTC)", "Position", "Evidence batches", "Bracketing observations"],
      message.crossings.map((c) => ({
        className: `crossing-${c.status}`,
        cells: [
          element("code", c.id),
          c.vessel_id,
          c.direction,
          c.reason ? `${c.status}: ${c.reason}` : c.status,
          crossingTime(c),
          intervalText(c.window),
          formatPosition(c.position[0], c.position[1]),
          ids(c.evidence_batch_ids),
          ids([c.before_observation_id, c.after_observation_id]),
        ],
      })),
    );
  }
  const coverage = document.getElementById("transit-coverage");
  if (coverage instanceof HTMLTableElement) {
    fillTable(
      coverage,
      "Coverage rows cited by these counts.",
      ["Coverage", "Interval (UTC)", "State", "Reason", "Capture batch", "Received by EYE (UTC)"],
      message.coverage.map((c) => ({
        className: `state-${c.state}`,
        cells: [element("code", c.id), intervalText(c.interval), c.state, c.reason ?? "none", element("code", c.batch_id), c.received_time],
      })),
    );
  }
}

/** Counts are unavailable: say so, and show no number that could be read as zero. */
export function renderTransitsUnavailable(reason: string): void {
  const summary = document.getElementById("transit-summary");
  if (summary) definitions(summary, [["Transit counts", `Unknown: ${reason}`]]);
  for (const id of ["transit-counts", "transit-crossings", "transit-coverage"]) {
    const table = document.getElementById(id);
    if (table instanceof HTMLTableElement) {
      table.replaceChildren();
      table.createCaption().textContent = "Unavailable: counts are unknown, not zero.";
    }
  }
}

export function renderTracks(tracks: readonly Track[]): void {
  const table = document.getElementById("track-facts");
  if (!(table instanceof HTMLTableElement)) return;
  fillTable(
    table,
    "Tracks in view: last observed position and times, from the stored evidence.",
    ["Source record", "Track ID", "Kind", "Source", "Display type", "Resolved positions", "Last observed (UTC)", "Last received by EYE (UTC)", "Last position", "Unresolved claims", "Quality flags"],
    [...tracks]
      .sort((a, b) =>
        a.source.localeCompare(b.source) || a.source_record_id.localeCompare(b.source_record_id) || a.kind.localeCompare(b.kind))
      .map((track) => {
        const f = trackFacts(track);
        const claims = f.claims.length === 0 ? document.createTextNode("none") : list(f.claims);
        return {
          className: f.claims.length > 0 ? "has-conflicts" : "",
          cells: [f.record, element("code", f.id), f.kind, f.source, f.displayType, f.points, f.lastObserved, f.lastReceived, f.lastPosition, claims, f.flags],
        };
      }),
  );
}

/** Where event-report coverage is shown instead of the general coverage table. */
export const EVENT_COVERAGE_POINTER =
  "Event-report coverage is not listed here: its intervals are reporting windows and its per-capture " +
  "counts must not be added together, so it is shown with those labels under DESK, world events, " +
  "Event-report coverage.";

/** An event-report row: shown only in the labelled event panel, never here. */
function isEventCoverage(c: Coverage): boolean {
  return c.metric.name === "event_cases_in_view" || c.interval_kind !== undefined || c.batch_id !== undefined;
}

export function renderCoverage(coverage: readonly Coverage[]): void {
  const table = document.getElementById("coverage-facts");
  if (!(table instanceof HTMLTableElement)) return;
  fillTable(
    table,
    `Coverage in view. A missing or failed capture is unknown, never zero. ${EVENT_COVERAGE_POINTER}`,
    ["Interval (UTC)", "Layer", "State", "Metric", "Reason"],
    coverage
      .filter((c) => !isEventCoverage(c))
      .sort((a, b) => compareTime(a.interval.start, b.interval.start) || a.layer.localeCompare(b.layer))
      .map((c) => ({
        className: `state-${c.state}`,
        cells: [intervalText(c.interval), c.layer, c.state, coverageMetric(c), c.reason ?? "none"],
      })),
    `No track coverage in view. ${EVENT_COVERAGE_POINTER}`,
  );
}

/**
 * World events: one row per case. The reported location and the last
 * observed position are separate columns and never merged. A case that breaks
 * a consistency rule is refused in its row, not shown as if it were valid.
 */
export function renderEvents(events: readonly EventCase[], coverage: readonly Coverage[], layers: readonly string[]): void {
  const lines = eventCoverageText(coverage, layers);
  const target = document.getElementById("event-coverage");
  if (target) target.replaceChildren(list(lines));
  const measured = reportsMeasured(coverage, layers);
  const table = document.getElementById("event-facts");
  if (!(table instanceof HTMLTableElement)) return;
  const rows = [...events]
    .sort((a, b) => a.layer.localeCompare(b.layer) || a.source.localeCompare(b.source) || a.case_id.localeCompare(b.case_id))
    .map((event) => {
      const errors = eventErrors(event);
      if (errors.length > 0) {
        return {
          cells: [`${event.source_label}: ${event.case_id}`, `Refused an inconsistent case (${errors[0] ?? ""})`,
            "", "", "", "", "", ""],
          className: `event refused`,
        };
      }
      const current = currentClaim(event);
      const shown = current ?? event.claims.find((c) => c.is_current === null) ?? event.claims[event.claims.length - 1];
      const reported = event.standing === "unresolved"
        ? list(event.claims.filter((c) => c.is_current === null).map((c) => locationText(c.reported_event_location)))
        : shown ? locationText(shown.reported_event_location) : "";
      return {
        cells: [
          `${event.source_label}: ${event.case_id}`,
          eventAssessment(event),
          shown ? `${kindLabel(shown.kind)} (${event.layer})` : event.layer,
          reported,
          shown ? eventTimeText(shown) : "",
          lastObservedText(event),
          shown ? `${shown.evidence_ref ?? "none (motion data)"}; batches ${shown.evidence_batch_ids.join(", ")}` : "",
          list(claimHistory(event)),
        ],
        className: `event standing-${event.standing}`,
      };
    });
  fillTable(
    table,
    "World events: sourced reports, review candidates and their history",
    ["Case", "Assessment", "Kind", "Reported event location", "Event time", "Last observed position (track)",
      "Evidence", "History (every version)"],
    rows,
    measured
      ? "No event reports in this view: the captures above found none published in their reporting windows. " +
        "Whether any event occurred is Unknown: a report published later can still add one."
      : "No event cases shown. Reports are unknown for part of this view (see event-report coverage above), " +
        "and whether any event occurred is Unknown.",
  );
}
