// Runtime validator for the EYE wire schema (browser side).
// Mirrors backend/eye/wire/validate.py over the same keyword subset; tests run
// both over a shared corpus and compare them with a reference implementation.

import { WIRE_SCHEMA_JSON } from "./generated/wire-schema.js";
import type { ClientMessage, ServerMessage } from "./generated/wire-types.js";

export const MAX_MESSAGE_BYTES = 1_048_576;
const MAX_ERRORS = 20;

type Entry = "ServerMessage" | "ClientMessage";
interface EntryTypes { ServerMessage: ServerMessage; ClientMessage: ClientMessage }

interface SchemaNode {
  $ref?: string;
  oneOf?: SchemaNode[];
  const?: unknown;
  enum?: unknown[];
  type?: string | string[];
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  items?: SchemaNode | false;
  prefixItems?: SchemaNode[];
  format?: string;
  properties?: Record<string, SchemaNode>;
  required?: string[];
  additionalProperties?: boolean;
}

const schema = JSON.parse(WIRE_SCHEMA_JSON) as { $defs: Record<string, SchemaNode> };
const patterns = new Map<string, RegExp>();

export class WireValidationError extends Error {
  constructor(readonly errors: string[]) {
    super(errors.join("; "));
  }
}

function typeMatches(value: unknown, name: string): boolean {
  switch (name) {
    case "null": return value === null;
    case "boolean": return typeof value === "boolean";
    case "string": return typeof value === "string";
    case "object": return typeof value === "object" && value !== null && !Array.isArray(value);
    case "array": return Array.isArray(value);
    case "integer": return typeof value === "number" && Number.isInteger(value);
    case "number": return typeof value === "number" && Number.isFinite(value);
    default: return false;
  }
}

function same(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function patternOk(pattern: string, value: string): boolean {
  let compiled = patterns.get(pattern);
  if (!compiled) {
    compiled = new RegExp(pattern);
    patterns.set(pattern, compiled);
  }
  return compiled.test(value);
}

const DATE_TIME = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,6})?Z$/;

/** RFC 3339 UTC instant on a real calendar date (leap years; no leap seconds). */
export function validDateTime(value: string): boolean {
  const match = DATE_TIME.exec(value);
  if (!match) return false;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number) as
    [number, number, number, number, number, number];
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  return days !== undefined && day >= 1 && day <= days && hour <= 23 && minute <= 59 && second <= 59;
}

function resolve(ref: string): SchemaNode {
  const node = schema.$defs[ref.split("/").pop() ?? ""];
  if (!node) throw new Error(`dangling schema reference ${ref}`);
  return node;
}

function check(value: unknown, node: SchemaNode, path: string, errors: string[]): void {
  if (errors.length >= MAX_ERRORS) return;
  if (node.$ref !== undefined) check(value, resolve(node.$ref), path, errors);
  if (node.oneOf !== undefined) {
    const matches = node.oneOf.filter((option) => {
      const sub: string[] = [];
      check(value, option, "$", sub);
      return sub.length === 0;
    }).length;
    if (matches !== 1) {
      errors.push(`${path}: must match exactly one allowed form (matched ${matches})`);
      return;
    }
  }
  if ("const" in node && !same(value, node.const)) errors.push(`${path}: must equal ${JSON.stringify(node.const)}`);
  if (node.enum !== undefined && !node.enum.some((option) => same(value, option))) {
    errors.push(`${path}: must be one of ${JSON.stringify(node.enum)}`);
  }
  if (node.type !== undefined) {
    const names = typeof node.type === "string" ? [node.type] : node.type;
    if (!names.some((name) => typeMatches(value, name))) {
      errors.push(`${path}: must be of type ${names.join(" or ")}`);
      return;
    }
  }
  if (typeof value === "string") {
    const length = [...value].length; // code points, as in JSON Schema and Python
    if (node.minLength !== undefined && length < node.minLength) errors.push(`${path}: shorter than ${node.minLength}`);
    if (node.maxLength !== undefined && length > node.maxLength) errors.push(`${path}: longer than ${node.maxLength}`);
    if (node.pattern !== undefined && !patternOk(node.pattern, value)) errors.push(`${path}: does not match the required format`);
    else if (node.format === "date-time" && !validDateTime(value)) errors.push(`${path}: not a real calendar date and time`);
  }
  if (typeMatches(value, "number")) {
    const n = value as number;
    if (node.minimum !== undefined && n < node.minimum) errors.push(`${path}: below minimum ${node.minimum}`);
    if (node.maximum !== undefined && n > node.maximum) errors.push(`${path}: above maximum ${node.maximum}`);
  }
  if (Array.isArray(value)) {
    if (node.minItems !== undefined && value.length < node.minItems) errors.push(`${path}: fewer than ${node.minItems} items`);
    if (node.maxItems !== undefined && value.length > node.maxItems) {
      errors.push(`${path}: more than ${node.maxItems} items`);
      return;
    }
    const prefix = node.prefixItems ?? [];
    prefix.forEach((child, index) => {
      if (index < value.length) check(value[index], child, `${path}[${index}]`, errors);
    });
    const items = node.items;
    if (items === false && value.length > prefix.length) errors.push(`${path}: at most ${prefix.length} items`);
    else if (items !== undefined && items !== false) {
      for (let index = prefix.length; index < value.length; index++) check(value[index], items, `${path}[${index}]`, errors);
    }
  }
  if (typeMatches(value, "object")) {
    const record = value as Record<string, unknown>;
    const properties = node.properties ?? {};
    for (const name of node.required ?? []) {
      if (!Object.hasOwn(record, name)) errors.push(`${path}: missing required field ${JSON.stringify(name)}`);
    }
    if (node.additionalProperties === false) {
      for (const name of Object.keys(record)) {
        if (!Object.hasOwn(properties, name)) errors.push(`${path}: unexpected field ${JSON.stringify(name)}`);
      }
    }
    for (const [name, child] of Object.entries(properties)) {
      if (Object.hasOwn(record, name)) check(record[name], child, `${path}.${name}`, errors);
    }
  }
}

export function wireErrors(value: unknown, entry: Entry): string[] {
  const errors: string[] = [];
  const node = schema.$defs[entry];
  if (!node) throw new Error(`unknown entry point ${entry}`);
  check(value, node, "$", errors);
  return errors.slice(0, MAX_ERRORS);
}

/** Parse and validate one message; throws WireValidationError if it is invalid. */
export function parseMessage<E extends Entry>(text: string, entry: E, maxBytes = MAX_MESSAGE_BYTES): EntryTypes[E] {
  const size = new TextEncoder().encode(text).length;
  if (size > maxBytes) throw new WireValidationError([`$: message is ${size} bytes, limit ${maxBytes}`]);
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw new WireValidationError([`$: not valid JSON (${String(error)})`]);
  }
  const errors = wireErrors(value, entry);
  if (errors.length > 0) throw new WireValidationError(errors);
  return value as EntryTypes[E];
}
