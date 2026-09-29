// Live feed over WebSocket: one snapshot, then sequenced deltas.
//
// Every message is validated against the wire schema before use. A delta whose
// previous_cursor or sequence does not follow the last one applied is a gap:
// the client stops applying deltas and subscribes again with its last good
// cursor, then rebuilds its state from the fresh snapshot. A dropped connection
// is retried with capped backoff and the same resume cursor. Nothing is queued
// on the client: each message is applied or refused as it arrives.
//
// A message in another wire version is not a gap: resubscribing or
// reconnecting would get the same answer again. The feed halts, keeps the data
// shown marked stale, and asks for a reload, which fetches a page that speaks
// the server's version.

import type {
  BBox,
  DeltaMessage,
  Interval,
  ServerMessage,
  SnapshotMessage,
  SubscribeMessage,
} from "./generated/wire-types.js";
import { WIRE_SCHEMA_VERSION } from "./generated/wire-schema.js";
import { MAX_MESSAGE_BYTES, parseMessage, WireValidationError, wireVersionOf } from "./wire-validate.js";

export type LiveState =
  | "connecting" | "live" | "resyncing" | "reconnecting" | "stale" | "incompatible" | "stopped";

export interface LiveHandlers {
  snapshot(message: SnapshotMessage): void;
  delta(message: DeltaMessage): void;
  status(state: LiveState, text: string): void;
}

export interface Subscription {
  bbox: BBox;
  interval: Interval;
  layers: SubscribeMessage["layers"];
}

const MAX_BACKOFF_MS = 30_000;

export class LiveFeed {
  private socket: WebSocket | null = null;
  private subscription: Subscription | null = null;
  private cursor: string | null = null;
  private sequence = 0;
  private awaitingSnapshot = true;
  private backoff = 1000;
  private timer: number | undefined;
  private halted = false;
  readonly counters = { snapshots: 0, deltas: 0, gaps: 0, reconnects: 0, refused: 0, versionMismatches: 0 };

  constructor(private readonly url: string, private readonly handlers: LiveHandlers) {}

  get lastCursor(): string | null {
    return this.cursor;
  }

  subscribe(subscription: Subscription, fresh: boolean): void {
    this.subscription = subscription;
    if (this.halted) return; // only a reload can speak the server's version
    if (fresh) this.cursor = null;
    if (this.socket?.readyState === WebSocket.OPEN) this.sendSubscribe();
    else if (!this.socket) this.connect();
  }

  stop(): void {
    window.clearTimeout(this.timer);
    this.subscription = null;
    this.socket?.close(1000, "page closed");
    this.socket = null;
    this.handlers.status("stopped", "Live updates stopped.");
  }

  private connect(): void {
    this.handlers.status(this.counters.reconnects > 0 ? "reconnecting" : "connecting", "Connecting to live updates…");
    const socket = new WebSocket(this.url);
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.backoff = 1000;
      this.sendSubscribe();
    });
    socket.addEventListener("message", (event: MessageEvent) => {
      if (typeof event.data !== "string") {
        this.refuse("binary message");
        return;
      }
      this.receive(event.data);
    });
    socket.addEventListener("close", () => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (!this.subscription || this.halted) return;
      this.counters.reconnects += 1;
      this.awaitingSnapshot = true;
      const wait = this.backoff;
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
      this.handlers.status(
        "reconnecting",
        `Live updates disconnected. Reconnecting in ${Math.round(wait / 1000)} s; ` +
          `data shown is as of cursor ${this.cursor ?? "none"}.`,
      );
      this.timer = window.setTimeout(() => this.connect(), wait);
    });
  }

  private sendSubscribe(): void {
    if (!this.subscription || this.socket?.readyState !== WebSocket.OPEN) return;
    this.awaitingSnapshot = true;
    const message: SubscribeMessage = {
      schema_version: "eye.wire/2",
      kind: "subscribe",
      bbox: this.subscription.bbox,
      interval: this.subscription.interval,
      layers: this.subscription.layers,
      resume_cursor: this.cursor,
    };
    this.socket.send(JSON.stringify(message));
  }

  private refuse(detail: string): void {
    this.counters.refused += 1;
    this.resync(`Refused an invalid message (${detail}); requesting a fresh snapshot.`);
  }

  private resync(text: string): void {
    this.handlers.status("resyncing", text);
    this.sendSubscribe();
  }

  /** Another wire version: stop, without resubscribing or reconnecting. */
  private halt(version: string): void {
    this.counters.versionMismatches += 1;
    this.halted = true;
    this.awaitingSnapshot = true;
    window.clearTimeout(this.timer);
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000, "wire version mismatch");
    this.handlers.status(
      "incompatible",
      `The server sent ${version} messages; this page speaks ${WIRE_SCHEMA_VERSION}. Live updates ` +
        `stopped and the data shown may be out of date as of cursor ${this.cursor ?? "none"}. ` +
        "Reload the page to continue.",
    );
  }

  private receive(text: string): void {
    const version = wireVersionOf(text);
    if (version !== null && version !== WIRE_SCHEMA_VERSION) {
      this.halt(version);
      return;
    }
    let message: ServerMessage;
    try {
      message = parseMessage(text, "ServerMessage", MAX_MESSAGE_BYTES);
    } catch (error) {
      this.refuse(error instanceof WireValidationError ? error.errors[0] ?? "invalid" : "invalid");
      return;
    }
    switch (message.kind) {
      case "snapshot":
        this.cursor = message.cursor;
        this.sequence = 0;
        this.awaitingSnapshot = false;
        this.counters.snapshots += 1;
        this.handlers.snapshot(message);
        this.handlers.status("live", `Live. Snapshot at cursor ${message.cursor}.`);
        return;
      case "delta":
        if (this.awaitingSnapshot) return; // stale: a fresh snapshot is on its way
        if (message.previous_cursor !== this.cursor || message.sequence !== this.sequence + 1) {
          this.counters.gaps += 1;
          this.awaitingSnapshot = true;
          this.resync(
            `Gap detected: expected an update after ${this.cursor ?? "none"}, received one after ` +
              `${message.previous_cursor}. Requesting a fresh snapshot.`,
          );
          return;
        }
        this.cursor = message.cursor;
        this.sequence = message.sequence;
        this.counters.deltas += 1;
        this.handlers.delta(message);
        this.handlers.status("live", `Live. Update ${message.sequence} applied at cursor ${message.cursor}.`);
        return;
      case "resync_required":
        this.awaitingSnapshot = true;
        this.handlers.status(
          "resyncing",
          message.reason === "expired_cursor"
            ? "Previous cursor expired; loading a fresh snapshot."
            : `Resynchronising (${message.reason}); loading a fresh snapshot.`,
        );
        return;
      case "error":
        // Either the live feed failed or the request was refused. Until a new
        // snapshot arrives nothing on the page is current: say so, and ignore
        // any delta, which would have no baseline.
        this.awaitingSnapshot = true;
        this.handlers.status(
          "stale",
          `Live updates stopped (${message.status}: ${message.error}). The data shown may be out of ` +
            `date as of cursor ${this.cursor ?? "none"}; it will refresh from a new snapshot.`,
        );
        return;
      default:
        this.refuse(`unexpected ${message.kind} message`);
    }
  }
}
