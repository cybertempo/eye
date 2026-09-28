// Renders the synthetic snapshot as plain tables (a placeholder for DESK).
// The response is validated against the wire schema before use, and values
// are inserted with textContent only, never as HTML.

import type { CoverageMeasured, CoverageMissing, SnapshotMessage } from "./generated/wire-types.js";
import { parseMessage, WireValidationError } from "./wire-validate.js";

function fillTable(id: string, headings: string[], rows: string[][]): void {
  const table = document.getElementById(id);
  if (!(table instanceof HTMLTableElement)) return;
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  for (const h of headings) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const cell of row) tr.insertCell().textContent = cell;
  }
}

function metricText(coverage: CoverageMeasured | CoverageMissing): string {
  // A missing feed is unknown, never zero; the schema forbids a number here.
  const value = coverage.metric.value;
  return value === null ? "unknown (no data)" : String(value);
}

function render(data: SnapshotMessage): void {
  fillTable("coverage", ["Layer", "Interval", "State", "Metric", "Reason"], data.coverage.map((c) => [
    c.layer, `${c.interval.start} to ${c.interval.end}`, c.state,
    `${c.metric.name}: ${metricText(c)}`, c.reason ?? "",
  ]));
  fillTable("tracks", ["Track", "Kind", "Type", "Points", "Last observed", "Last received"], data.tracks.map((t) => [
    t.id, t.kind, t.display_type, String(t.points.length),
    t.points.at(-1)?.observed_time ?? "unknown", t.points.at(-1)?.received_time ?? "unknown",
  ]));
  fillTable("events", ["Event", "Kind", "Status", "Event time", "Revisions", "Replayed"], data.events.map((e) => [
    e.id, e.kind, e.status, e.event_time, String(e.revisions.length), e.replayed ? "yes" : "no",
  ]));
}

async function main(): Promise<void> {
  const status = document.getElementById("status");
  try {
    const response = await fetch("/api/v0/snapshot", { credentials: "same-origin" });
    const message = parseMessage(await response.text(), "ServerMessage");
    if (message.kind === "error") throw new Error(`server error ${message.status}: ${message.error}`);
    if (message.kind !== "snapshot") throw new Error(`expected a snapshot, received ${message.kind}`);
    render(message);
    if (status) status.textContent = `Synthetic snapshot loaded (${message.schema_version}, cursor ${message.cursor}).`;
  } catch (error) {
    const detail = error instanceof WireValidationError ? `invalid message: ${error.errors.join("; ")}` : String(error);
    if (status) status.textContent = `Snapshot unavailable: ${detail}`;
  }
}

void main();
