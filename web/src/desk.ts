// DESK: observed transits for one versioned count line, with the coverage,
// uncertainty, timestamps, evidence IDs and source labels behind each count.
// Values are inserted with textContent only, never as HTML.

import type { Coverage, Track, TransitsMessage } from "./generated/wire-types.js";
import {
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
    cell.textContent = "No rows for this interval.";
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
    ["Source record", "Track ID", "Kind", "Source", "Display type", "Positions", "Last observed (UTC)", "Last received by EYE (UTC)", "Last position", "Quality flags"],
    [...tracks]
      .sort((a, b) => a.source.localeCompare(b.source) || a.source_record_id.localeCompare(b.source_record_id))
      .map((track) => {
        const f = trackFacts(track);
        return {
          cells: [f.record, element("code", f.id), f.kind, f.source, f.displayType, f.points, f.lastObserved, f.lastReceived, f.lastPosition, f.flags],
        };
      }),
  );
}

export function renderCoverage(coverage: readonly Coverage[]): void {
  const table = document.getElementById("coverage-facts");
  if (!(table instanceof HTMLTableElement)) return;
  fillTable(
    table,
    "Coverage in view. A missing or failed capture is unknown, never zero.",
    ["Interval (UTC)", "Layer", "State", "Metric", "Reason"],
    [...coverage]
      .sort((a, b) => a.interval.start.localeCompare(b.interval.start) || a.layer.localeCompare(b.layer))
      .map((c) => ({
        className: `state-${c.state}`,
        cells: [intervalText(c.interval), c.layer, c.state, coverageMetric(c), c.reason ?? "none"],
      })),
  );
}
