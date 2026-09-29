// THEATRE globe: a token-free orthographic view drawn on a 2D canvas.
//
// No basemap, imagery or map service is used; the globe shows a graticule, the
// requested area, the count line and the synthetic tracks. Presets change only
// how much is drawn (graticule density, canvas resolution, track sampling);
// they never change a stored position, a count or any text in the fact tables.
// This is an illustrative view, not a measurement surface.

import { routeRuns } from "./facts.js";
import type { BBox, Position, Track, TrackPoint } from "./generated/wire-types.js";

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
  private data: GlobeData = { tracks: [], area: null, line: null };
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
    // What was stroked, by track id: the observed times of each drawn run.
    this.canvas.dataset.routeRuns = JSON.stringify(drawnRuns);
  }
}
