// Renders the synthetic snapshot as plain tables (a placeholder for DESK).
// Values are inserted with textContent only, never as HTML. Package 1 replaces
// these hand-written types with types generated from the versioned wire schema.

interface Point { observed_time: string; received_time: string; lon: number; lat: number }
interface Track { id: string; kind: string; source: string; display_type: string; points: Point[] }
interface EventRevision { revision: number; published_time: string; status: string }
interface EyeEvent {
  id: string; kind: string; source: string; status: string;
  event_time: string; replayed: boolean; revisions: EventRevision[];
}
interface Coverage {
  layer: string; state: string; interval: { start: string; end: string };
  metric: { name: string; value: number | null };
}
interface Snapshot { synthetic: boolean; tracks: Track[]; events: EyeEvent[]; coverage: Coverage[] }

function isSnapshot(value: unknown): value is Snapshot {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return v["synthetic"] === true && Array.isArray(v["tracks"]) &&
    Array.isArray(v["events"]) && Array.isArray(v["coverage"]);
}

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

function metricText(value: number | null): string {
  // A missing feed is unknown, never zero.
  return value === null ? "unknown (no data)" : String(value);
}

async function main(): Promise<void> {
  const status = document.getElementById("status");
  try {
    const response = await fetch("/api/v0/snapshot", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data: unknown = await response.json();
    if (!isSnapshot(data)) throw new Error("response is not a synthetic snapshot");
    fillTable("coverage", ["Layer", "Interval", "State", "Metric"], data.coverage.map((c) => [
      c.layer, `${c.interval.start} to ${c.interval.end}`, c.state,
      `${c.metric.name}: ${metricText(c.metric.value)}`,
    ]));
    fillTable("tracks", ["Track", "Kind", "Type", "Points", "Last observed"], data.tracks.map((t) => [
      t.id, t.kind, t.display_type, String(t.points.length),
      t.points.at(-1)?.observed_time ?? "unknown",
    ]));
    fillTable("events", ["Event", "Kind", "Status", "Event time", "Revisions", "Replayed"], data.events.map((e) => [
      e.id, e.kind, e.status, e.event_time, String(e.revisions.length), e.replayed ? "yes" : "no",
    ]));
    if (status) status.textContent = "Synthetic snapshot loaded.";
  } catch (error) {
    if (status) status.textContent = `Snapshot unavailable: ${String(error)}`;
  }
}

void main();
