// THEATRE globe: a token-free orthographic view drawn on a 2D canvas.
//
// No basemap, imagery or map service is used; the globe shows a graticule, the
// requested area, the count line and the synthetic tracks. Presets change only
// how much is drawn (graticule density, canvas resolution, track sampling);
// they never change a stored position, a count or any text in the fact tables.
// This is an illustrative view, not a measurement surface.

import { eventErrors, mediaDrawable, mediaErrors, shownVersion, routeRuns } from "./facts.js";
import type {
  BBox,
  EventCase,
  EventClaim,
  MediaItem,
  Position,
  Track,
  TrackPoint,
} from "./generated/wire-types.js";

export type Preset = "low" | "balanced" | "high";

interface PresetSettings {
  label: string;
  graticuleDegrees: number;
  segments: number;
  maxPixelRatio: number;
  pointStride: number;
}

export const PRESETS: Record<Preset, PresetSettings> = {
  low: { label: "Low", graticuleDegrees: 30, segments: 24, maxPixelRatio: 1, pointStride: 4 },
  balanced: { label: "Balanced", graticuleDegrees: 15, segments: 48, maxPixelRatio: 1.5, pointStride: 2 },
  high: { label: "High", graticuleDegrees: 5, segments: 96, maxPixelRatio: 2, pointStride: 1 },
};

const MIN_ZOOM = 1;
const MAX_ZOOM = 50_000;
const RAD = Math.PI / 180;

export interface GlobeData {
  tracks: readonly Track[];
  events: readonly EventCase[];
  media: readonly MediaItem[];
  interval: { start: string; end: string };
  area: BBox | null;
  line: readonly [Position, Position] | null;
}

/**
 * Thin one run of the route for drawing. Every run keeps its first and last
 * point, and runs are thinned separately, so a lower preset draws fewer points
 * but never joins across a break in the route.
 */
export function sampleRun(run: readonly TrackPoint[], stride: number): TrackPoint[] {
  return run.filter((_, i, all) => i % stride === 0 || i === all.length - 1);
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

export class Globe {
  private lon = 0;
  private lat = 0;
  private zoom = 1;
  private preset: Preset = "balanced";
  private data: GlobeData = {
    tracks: [],
    events: [],
    media: [],
    interval: { start: "1970-01-01T00:00:00Z", end: "1970-01-01T00:00:00Z" },
    area: null,
    line: null,
  };
  private home: { lon: number; lat: number; zoom: number } = { lon: 0, lat: 0, zoom: 1 };

  constructor(private readonly canvas: HTMLCanvasElement, private readonly onChange: (text: string) => void) {}

  get presetName(): Preset {
    return this.preset;
  }

  setPreset(preset: Preset): void {
    this.preset = preset;
    this.draw();
  }

  setData(data: GlobeData, fit: boolean): void {
    this.data = data;
    if (fit) {
      const extent = this.extent();
      if (extent) this.fit(extent);
      else if (data.area) this.fit(data.area);
    }
    this.draw();
  }

  /** Bounding box of the drawn tracks and count line, or null if there are none. */
  private extent(): BBox | null {
    const points: (readonly [number, number])[] = [];
    for (const track of this.data.tracks) {
      for (const p of track.points) points.push([p.lon, p.lat]);
      for (const c of track.conflicts ?? []) for (const claim of c.claims) points.push([claim.lon, claim.lat]);
    }
    if (this.data.line) points.push(...this.data.line);
    if (points.length === 0) return null;
    // Events do not move the fitted view: they are drawn where they fall.
    const lons = points.map((p) => p[0]);
    const lats = points.map((p) => p[1]);
    const pad = 0.05;
    return [Math.min(...lons) - pad, Math.min(...lats) - pad, Math.max(...lons) + pad, Math.max(...lats) + pad];
  }

  fit(area: BBox): void {
    const [west, south, east, north] = area;
    const span = Math.max(east - west, north - south, 1e-6);
    this.home = { lon: (west + east) / 2, lat: (south + north) / 2, zoom: clamp(100 / span, MIN_ZOOM, MAX_ZOOM) };
    this.reset();
  }

  reset(): void {
    ({ lon: this.lon, lat: this.lat, zoom: this.zoom } = this.home);
    this.draw();
  }

  rotate(stepsEast: number, stepsNorth: number): void {
    const step = 20 / this.zoom;
    this.lon = ((this.lon + stepsEast * step + 540) % 360) - 180;
    this.lat = clamp(this.lat + stepsNorth * step, -89, 89);
    this.draw();
  }

  zoomBy(factor: number): void {
    this.zoom = clamp(this.zoom * factor, MIN_ZOOM, MAX_ZOOM);
    this.draw();
  }

  describe(): string {
    const ns = this.lat >= 0 ? "N" : "S";
    const ew = this.lon >= 0 ? "E" : "W";
    const tracks = this.data.tracks.length;
    return (
      `Globe centred on ${Math.abs(this.lat).toFixed(3)}°${ns} ${Math.abs(this.lon).toFixed(3)}°${ew}, ` +
      `zoom ×${this.zoom.toFixed(0)}, ${PRESETS[this.preset].label} preset. ` +
      `${tracks} track${tracks === 1 ? "" : "s"}${this.data.line ? " and the count line" : ""} drawn. ` +
      "Graticule only: no basemap. Illustrative, not for measurement."
    );
  }

  /**
   * Event cases: the reported location of the current claim (or of every
   * conflicting latest claim) at its stated precision, and, separately, a
   * linked track's last observed position. The two are never joined by a line.
   * Review candidates are grey and dashed; sourced reports are solid.
   */
  private paintEvents(
    ctx: CanvasRenderingContext2D, radius: number, colour: (name: string, fallback: string) => string,
  ): void {
    const marks: Record<string, { style: string; reported: string[]; lastObserved: boolean }> = {};
    for (const event of this.data.events) {
      if (eventErrors(event).length > 0) continue; // refused cases are not drawn
      const shown = event.standing === "unresolved"
        ? event.claims.filter((c) => c.is_current === null)
        : event.claims.filter((c) => c.is_current === true);
      const candidate = event.standing === "review_candidate";
      ctx.strokeStyle = candidate ? colour("--globe-event-candidate", "#6b6b6b") : colour("--globe-event", "#b35900");
      ctx.setLineDash(candidate || event.standing === "unresolved" ? [4, 3] : []);
      ctx.lineWidth = 2;
      for (const claim of shown) this.drawLocation(ctx, claim, radius);
      ctx.setLineDash([]);
      const last = event.last_observed_position;
      if (last) {
        const p = this.project(last.lon, last.lat, radius);
        if (p) {
          ctx.fillStyle = colour("--globe-last-seen", "#0b5cad");
          ctx.fillRect(p[0] - 3, p[1] - 3, 6, 6);
        }
      }
      marks[event.id] = {
        style: event.standing,
        reported: shown.map((c) => c.reported_event_location.type),
        lastObserved: last !== null,
      };
    }
    this.canvas.dataset.eventMarks = JSON.stringify(marks);
  }

  /**
   * News and media: only a place the source itself states as the event's
   * place is drawn, as a dotted circle at its stated precision, never as a
   * point, a live marker or a crowd. A publisher's location, a place merely
   * mentioned, an automated geocode, a retracted item and an image or video
   * captured (by its creator's claim) outside the view are not drawn.
   */
  private paintMedia(
    ctx: CanvasRenderingContext2D, radius: number, colour: (name: string, fallback: string) => string,
  ): void {
    const marks: Record<string, { style: string; precision_m: number }> = {};
    ctx.strokeStyle = colour("--globe-media", "#6a3d9a");
    ctx.setLineDash([1, 3]);
    ctx.lineWidth = 1.5;
    for (const item of this.data.media) {
      if (mediaErrors(item).length > 0) continue; // refused items are not drawn
      const version = shownVersion(item);
      if (!version || version.place === null || !mediaDrawable(item.kind, version, this.data.interval)) continue;
      const [lon, lat] = version.place.coords;
      const p = this.project(lon, lat, radius);
      if (!p) continue;
      const edge = this.project(lon, Math.min(90, lat + version.place.precision_m / 111_320), radius);
      const size = edge ? Math.max(6, Math.hypot(edge[0] - p[0], edge[1] - p[1])) : 6;
      ctx.beginPath();
      ctx.arc(p[0], p[1], size, 0, Math.PI * 2);
      ctx.stroke();
      marks[item.id] = { style: "approximate_area", precision_m: version.place.precision_m };
    }
    ctx.setLineDash([]);
    this.canvas.dataset.mediaMarks = JSON.stringify(marks);
  }

  private drawLocation(ctx: CanvasRenderingContext2D, claim: EventClaim, radius: number): void {
    const location = claim.reported_event_location;
    if (location.type === "point") {
      const [lon, lat] = location.coords;
      const p = this.project(lon, lat, radius);
      if (!p) return;
      // Draw the stated precision, never a sharper point than the source gave.
      const edge = this.project(lon, Math.min(90, lat + location.precision_m / 111_320), radius);
      const size = edge ? Math.max(4, Math.hypot(edge[0] - p[0], edge[1] - p[1])) : 4;
      ctx.beginPath();
      ctx.arc(p[0], p[1], size, 0, Math.PI * 2);
      ctx.stroke();
      return;
    }
    this.path(ctx, location.coords.map((c) => [c[0], c[1]] as const), radius);
    if (location.type === "segment" && location.direction === "forward" && location.coords.length >= 2) {
      // An arrowhead at the end: only this direction of travel is affected.
      const a = location.coords[location.coords.length - 2];
      const b = location.coords[location.coords.length - 1];
      const pa = a ? this.project(a[0], a[1], radius) : null;
      const pb = b ? this.project(b[0], b[1], radius) : null;
      if (pa && pb) {
        const angle = Math.atan2(pb[1] - pa[1], pb[0] - pa[0]);
        ctx.beginPath();
        ctx.moveTo(pb[0], pb[1]);
        ctx.lineTo(pb[0] - 8 * Math.cos(angle - 0.4), pb[1] - 8 * Math.sin(angle - 0.4));
        ctx.moveTo(pb[0], pb[1]);
        ctx.lineTo(pb[0] - 8 * Math.cos(angle + 0.4), pb[1] - 8 * Math.sin(angle + 0.4));
        ctx.stroke();
      }
    }
  }

  private project(lon: number, lat: number, radius: number): [number, number] | null {
    const phi = lat * RAD;
    const lambda = (lon - this.lon) * RAD;
    const phi0 = this.lat * RAD;
    const cosc = Math.sin(phi0) * Math.sin(phi) + Math.cos(phi0) * Math.cos(phi) * Math.cos(lambda);
    if (cosc < 0) return null;
    const x = radius * Math.cos(phi) * Math.sin(lambda);
    const y = radius * (Math.cos(phi0) * Math.sin(phi) - Math.sin(phi0) * Math.cos(phi) * Math.cos(lambda));
    return [x, -y];
  }

  private path(ctx: CanvasRenderingContext2D, points: readonly (readonly [number, number])[], radius: number): void {
    let drawing = false;
    ctx.beginPath();
    for (const [lon, lat] of points) {
      const p = this.project(lon, lat, radius);
      if (!p) {
        drawing = false;
        continue;
      }
      if (drawing) ctx.lineTo(p[0], p[1]);
      else ctx.moveTo(p[0], p[1]);
      drawing = true;
    }
    ctx.stroke();
  }

  draw(): void {
    const settings = PRESETS[this.preset];
    const ratio = Math.min(window.devicePixelRatio || 1, settings.maxPixelRatio);
    const width = Math.max(200, this.canvas.clientWidth || 600);
    const height = Math.max(200, this.canvas.clientHeight || 400);
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    const ctx = this.canvas.getContext("2d");
    if (ctx) this.paint(ctx, width, height, ratio, settings);
    this.canvas.dataset.preset = this.preset;
    this.onChange(this.describe());
  }

  private paint(
    ctx: CanvasRenderingContext2D, width: number, height: number, ratio: number, settings: PresetSettings,
  ): void {
    const styles = getComputedStyle(this.canvas);
    const colour = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback;
    ctx.setTransform(ratio, 0, 0, ratio, width * ratio / 2, height * ratio / 2);
    ctx.clearRect(-width, -height, width * 2, height * 2);
    const radius = (Math.min(width, height) / 2) * 0.9 * this.zoom;

    ctx.fillStyle = colour("--globe-sea", "#dfe8f1");
    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = colour("--globe-grid", "#9aa8b6");
    ctx.lineWidth = 0.6;
    const g = settings.graticuleDegrees;
    for (let lon = -180; lon < 180; lon += g) {
      const pts: [number, number][] = [];
      for (let i = 0; i <= settings.segments; i++) pts.push([lon, -90 + (180 * i) / settings.segments]);
      this.path(ctx, pts, radius);
    }
    for (let lat = -90 + g; lat < 90; lat += g) {
      const pts: [number, number][] = [];
      for (let i = 0; i <= settings.segments * 2; i++) pts.push([-180 + (360 * i) / (settings.segments * 2), lat]);
      this.path(ctx, pts, radius);
    }

    if (this.data.area) {
      const [w, s, e, n] = this.data.area;
      ctx.strokeStyle = colour("--globe-area", "#7a4b00");
      ctx.setLineDash([6, 4]);
      ctx.lineWidth = 1.5;
      this.path(ctx, [[w, s], [e, s], [e, n], [w, n], [w, s]], radius);
      ctx.setLineDash([]);
    }
    if (this.data.line) {
      ctx.strokeStyle = colour("--globe-line", "#b00020");
      ctx.lineWidth = 3;
      this.path(ctx, this.data.line, radius);
    }
    let routePoints = 0;
    let claimMarkers = 0;
    const drawnRuns: Record<string, string[][]> = {};
    for (const track of this.data.tracks) {
      ctx.strokeStyle = colour(track.kind === "flight" ? "--globe-flight" : "--globe-vessel", "#0b5cad");
      ctx.lineWidth = 2;
      // The route joins resolved positions only, and breaks at every
      // contested time between them; each run is a separate path.
      const runs = routeRuns(track).map((run) => sampleRun(run, settings.pointStride));
      routePoints += track.points.length;
      for (const run of runs) this.path(ctx, run.map((p) => [p.lon, p.lat] as const), radius);
      drawnRuns[track.id] = runs.map((run) => run.map((p) => p.observed_time));
      // Contested claims are separate hollow markers, never joined to each
      // other or to the route.
      ctx.strokeStyle = colour("--globe-conflict", "#b00020");
      for (const conflict of track.conflicts ?? []) {
        for (const claim of conflict.claims) {
          const p = this.project(claim.lon, claim.lat, radius);
          claimMarkers += 1;
          if (!p) continue;
          ctx.beginPath();
          ctx.arc(p[0], p[1], 5, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
    }
    this.canvas.dataset.routePoints = String(routePoints);
    this.canvas.dataset.claimMarkers = String(claimMarkers);
    this.paintEvents(ctx, radius, colour);
    this.paintMedia(ctx, radius, colour);
    // What was stroked, by track id: the observed times of each drawn run.
    this.canvas.dataset.routeRuns = JSON.stringify(drawnRuns);
  }
}
