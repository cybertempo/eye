// Test helper: run the compiled browser validator (web/dist) over corpus files.
// Usage: node wire_check.mjs <file.json>... ; each file is {entry, message}, or
// a raw text case {entry, raw}. Prints {"<file>": {"valid": bool, "errors": [...]}}.
import { readFileSync } from "node:fs";
import { parseMessage, WireValidationError } from "../../web/dist/wire-validate.js";

const results = {};
for (const file of process.argv.slice(2)) {
  const test = JSON.parse(readFileSync(file, "utf8"));
  const text = "raw" in test ? test.raw : JSON.stringify(test.message);
  try {
    parseMessage(text, test.entry, test.max_bytes);
    results[file] = { valid: true, errors: [] };
  } catch (error) {
    if (!(error instanceof WireValidationError)) throw error;
    results[file] = { valid: false, errors: error.errors };
  }
}
process.stdout.write(JSON.stringify(results));
