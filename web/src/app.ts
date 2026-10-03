// EYE browser client: THEATRE (globe) and DESK (observed transits).
//
// Loads the default view over REST, then follows it live over WebSocket. Every
// response is validated against the wire schema before use; an unavailable or
// invalid response is shown as unknown, never as zero or as an empty result.

import type {
  BBox,
  Coverage,
  DeltaMessage,
  EventCase,
  Interval,
  Position,
  SnapshotMessage,
  SubscribeMessage,
  Track,
} from "./generated/wire-types.js";
import {
  renderCoverage,
  renderEvents,
  renderTracks,
  renderTransits,
  renderTransitsPending,
  renderTransitsUnavailable,
} from "./desk.js";
import { transitsErrors } from "./facts.js";
import { Globe, type Preset, PRESETS } from "./globe.js";
import { LiveFeed, type LiveState } from "./live.js";
import { WIRE_SCHEMA_VERSION } from "./generated/wire-schema.js";
import { parseMessage, WireValidationError, wireVersionOf } from "./wire-validate.js";

const MAX_TRACKS = 1000; // client-side bound, equal to the server's wire limit
const MAX_COVERAGE = 1000;
const MAX_EVENTS = 1000; // client-side bound, equal to the server's wire limit
const TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

interface ViewState {
  area: BBox;
  interval: Interval;
  layers: SubscribeMessage["layers"];
  tracks: Map<string, Track>;
  events: Map<string, EventCase>;
  coverage: Map<string, Coverage>;
  line: readonly [Position, Position] | null;
}

const state: ViewState = {
  area: [-0.5, -0.5, 0.5, 0.5],
  interval: { start: "1970-01-01T00:00:00Z", end: "1970-01-01T01:00:00Z" },
  layers: ["flight", "vessel", "road"],
  tracks: new Map(),
  events: new Map(),
  coverage: new Map(),
  line: null,
};

function byId<T extends HTMLElement>(id: string, type: new () => T): T {
  const node = document.getElementById(id);
  if (!(node instanceof type)) throw new Error(`page is missing #${id}`);
  return node;
}

function setStatus(stateName: LiveState | "error" | "loading", text: string): void {
  const status = byId("live-status", HTMLElement);
  status.textContent = text;
  status.dataset.state = stateName;
  // Once a view is on the page, anything but a live feed (connecting,
  // resynchronising, reconnecting, stale, failed) means it may be out of date.
  // "live" is set only after a valid snapshot, or a delta that follows one.
  const stale = stateName !== "live" && (rendered || stateName === "error" || stateName === "incompatible");
  const banner = byId("stale-banner", HTMLElement);
  banner.hidden = !stale;
  document.body.classList.toggle("stale", stale);
}

function coverageKey(c: Coverage): string {
  return JSON.stringify(c);
}

let globe: Globe;
let rendered = false; // a view (REST or live snapshot) is on the page
let live: LiveFeed;
let transitsTimer: number | undefined;

function renderView(fit: boolean): void {
  const tracks = [...state.tracks.values()];
  const events = [...state.events.values()];
  globe.setData({ tracks, events, area: state.area, line: state.line }, fit);
  renderTracks(tracks);
  renderCoverage([...state.coverage.values()]);
  renderEvents(events, [...state.coverage.values()], state.layers);
  const view = byId("view-summary", HTMLElement);
  view.textContent =
    `Area [${state.area.join(", ")}], ${state.interval.start} to ${state.interval.end} (UTC), ` +
    `layers ${state.layers.join(", ")}.`;
}

function applySnapshot(message: SnapshotMessage, fit: boolean): void {
  const newWindow =
    message.interval.start !== state.interval.start || message.interval.end !== state.interval.end;
  rendered = true;
  state.area = message.area.bbox;
  state.interval = message.interval;
  state.tracks = new Map(message.tracks.map((t) => [t.id, t]));
  state.events = new Map(message.events.map((e) => [e.id, e]));
  state.coverage = new Map(message.coverage.map((c) => [coverageKey(c), c]));
  const notice = byId("notice", HTMLElement);
  notice.textContent = message.synthetic
    ? `SYNTHETIC DATA. ${message.notice ?? "Invented records only."}`
    : "Data not marked synthetic.";
  byId("cursor", HTMLElement).textContent = message.cursor;
  // The previous window's counts never sit beside this window's panels.
  if (newWindow) renderTransitsPending(state.interval);
  renderView(fit);
}

function applyDelta(message: DeltaMessage): void {
  for (const track of message.tracks_upserted) state.tracks.set(track.id, track);
  for (const c of message.coverage_upserted) state.coverage.set(coverageKey(c), c);
  for (const event of message.events_upserted) state.events.set(event.id, event);
  byId("cursor", HTMLElement).textContent = message.cursor;
  if (state.tracks.size > MAX_TRACKS || state.coverage.size > MAX_COVERAGE || state.events.size > MAX_EVENTS) {
    // Bounded client state: past the limit, rebuild from a fresh snapshot.
    state.tracks.clear();
    state.coverage.clear();
    state.events.clear();
    live.subscribe({ bbox: state.area, interval: state.interval, layers: state.layers }, true);
    return;
  }
  renderView(false);
}

async function fetchMessage(url: string): Promise<ReturnType<typeof parseMessage<"ServerMessage">>> {
  const response = await fetch(url, { credentials: "same-origin" });
  const text = await response.text();
  const version = wireVersionOf(text);
  if (version !== null && version !== WIRE_SCHEMA_VERSION) {
    throw new Error(`the server sent ${version}; this page speaks ${WIRE_SCHEMA_VERSION}. Reload the page`);
  }
  return parseMessage(text, "ServerMessage");
}

async function loadTransits(): Promise<void> {
  const interval = state.interval;
  const query = new URLSearchParams({ start: interval.start, end: interval.end });
  try {
    const message = await fetchMessage(`/api/v0/transits?${query.toString()}`);
    // A newer snapshot moved the view on: its own request will render.
    if (state.interval !== interval) return;
    if (message.kind === "error") {
      renderTransitsUnavailable(`server said ${message.status}: ${message.error}`);
      return;
    }
    if (message.kind !== "transits") {
      renderTransitsUnavailable(`unexpected ${message.kind} message`);
      return;
    }
    const errors = transitsErrors(message);
    if (errors.length > 0) {
      renderTransitsUnavailable(`refused an inconsistent response (${errors[0] ?? ""})`);
      return;
    }
    state.line = message.line.coords.length === 2
      ? [message.line.coords[0] as Position, message.line.coords[1] as Position]
      : null;
    renderTransits(message);
    renderView(false);
  } catch (error) {
    if (state.interval !== interval) return;
    const detail = error instanceof WireValidationError ? `invalid response: ${error.errors[0] ?? ""}` : "request failed";
    renderTransitsUnavailable(detail);
  }
}

function scheduleTransits(): void {
  window.clearTimeout(transitsTimer);
  transitsTimer = window.setTimeout(() => void loadTransits(), 200);
}

function setupTabs(): void {
  const tabs = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  const select = (tab: HTMLButtonElement, focus: boolean) => {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(other.getAttribute("aria-controls") ?? "");
      if (panel) panel.hidden = !selected;
    }
    if (focus) tab.focus();
    if (tab.id === "tab-theatre") globe.draw();
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab, false));
    tab.addEventListener("keydown", (event) => {
      const moves: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1 };
      let next: HTMLButtonElement | undefined;
      if (event.key in moves) next = tabs[(index + (moves[event.key] ?? 0) + tabs.length) % tabs.length];
      else if (event.key === "Home") next = tabs[0];
      else if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        select(next, true);
      }
    });
  });
}

function setupGlobeControls(): void {
  const canvas = byId("globe", HTMLCanvasElement);
  const actions: Record<string, () => void> = {
    "rotate-west": () => globe.rotate(-1, 0),
    "rotate-east": () => globe.rotate(1, 0),
    "rotate-north": () => globe.rotate(0, 1),
    "rotate-south": () => globe.rotate(0, -1),
    "zoom-in": () => globe.zoomBy(2),
    "zoom-out": () => globe.zoomBy(0.5),
    "reset-view": () => globe.reset(),
  };
  for (const [id, action] of Object.entries(actions)) byId(id, HTMLButtonElement).addEventListener("click", action);
  canvas.addEventListener("keydown", (event) => {
    const keys: Record<string, () => void> = {
      ArrowLeft: actions["rotate-west"] ?? (() => undefined),
      ArrowRight: actions["rotate-east"] ?? (() => undefined),
      ArrowUp: actions["rotate-north"] ?? (() => undefined),
      ArrowDown: actions["rotate-south"] ?? (() => undefined),
      "+": actions["zoom-in"] ?? (() => undefined),
      "=": actions["zoom-in"] ?? (() => undefined),
      "-": actions["zoom-out"] ?? (() => undefined),
      Home: actions["reset-view"] ?? (() => undefined),
      "0": actions["reset-view"] ?? (() => undefined),
    };
    const action = keys[event.key];
    if (action) {
      event.preventDefault();
      action();
    }
  });
  const preset = byId("preset", HTMLSelectElement);
  for (const [name, settings] of Object.entries(PRESETS)) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = settings.label;
    option.selected = name === globe.presetName;
    preset.append(option);
  }
  preset.addEventListener("change", () => globe.setPreset(preset.value as Preset));
  window.addEventListener("resize", () => globe.draw());
}

function setupViewForm(): void {
  const form = byId("view-form", HTMLFormElement);
  const start = byId("view-start", HTMLInputElement);
  const hours = byId("view-hours", HTMLInputElement);
  const error = byId("view-error", HTMLElement);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const count = Number(hours.value);
    if (!TIME.test(start.value) || Number.isNaN(Date.parse(start.value))) {
      error.textContent = "Start must be a UTC time such as 2026-02-01T12:00:00Z.";
      start.setAttribute("aria-invalid", "true");
      start.focus();
      return;
    }
    if (!Number.isInteger(count) || count < 1 || count > 168) {
      error.textContent = "Hours must be a whole number from 1 to 168.";
      hours.setAttribute("aria-invalid", "true");
      hours.focus();
      return;
    }
    start.removeAttribute("aria-invalid");
    hours.removeAttribute("aria-invalid");
    error.textContent = "";
    const end = new Date(Date.parse(start.value) + count * 3_600_000).toISOString().replace(".000Z", "Z");
    // The page keeps describing the view it shows until the requested view's
    // snapshot is applied; that snapshot then reloads the transit counts.
    live.subscribe({ bbox: state.area, interval: { start: start.value, end }, layers: state.layers }, true);
  });
}

function fillViewForm(): void {
  byId("view-start", HTMLInputElement).value = state.interval.start;
  const hours = (Date.parse(state.interval.end) - Date.parse(state.interval.start)) / 3_600_000;
  byId("view-hours", HTMLInputElement).value = String(Math.max(1, Math.round(hours)));
}

async function main(): Promise<void> {
  globe = new Globe(byId("globe", HTMLCanvasElement), (text) => {
    byId("globe", HTMLCanvasElement).setAttribute("aria-label", text);
    byId("globe-description", HTMLElement).textContent = text;
  });
  setupTabs();
  setupGlobeControls();
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  live = new LiveFeed(`${scheme}//${window.location.host}/api/v0/stream`, {
    snapshot: (message) => {
      applySnapshot(message, false);
      scheduleTransits();
    },
    delta: (message) => {
      applyDelta(message);
      scheduleTransits();
    },
    status: (name, text) => {
      setStatus(name, text);
      const counters = byId("live-status", HTMLElement).dataset;
      counters.gaps = String(live.counters.gaps);
      counters.reconnects = String(live.counters.reconnects);
      counters.snapshots = String(live.counters.snapshots);
      counters.deltas = String(live.counters.deltas);
      counters.versionMismatches = String(live.counters.versionMismatches);
    },
  });
  setupViewForm();
  setStatus("loading", "Loading the default view…");
  try {
    const message = await fetchMessage("/api/v0/snapshot");
    if (message.kind === "error") throw new Error(`server said ${message.status}: ${message.error}`);
    if (message.kind !== "snapshot") throw new Error(`expected a snapshot, received ${message.kind}`);
    state.layers = ["flight", "vessel", "road"];
    applySnapshot(message, true);
    fillViewForm();
    await loadTransits();
    live.subscribe({ bbox: state.area, interval: state.interval, layers: state.layers }, true);
  } catch (error) {
    const detail = error instanceof WireValidationError ? `invalid message: ${error.errors[0] ?? ""}` : String(error);
    setStatus("error", `View unavailable (${detail}). Nothing is shown as zero.`);
    renderTransitsUnavailable("the view could not be loaded");
  }
}

void main();
