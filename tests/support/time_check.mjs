// Test helper: order wire timestamps with the compiled browser comparator.
// Usage: node time_check.mjs '<json array of [a, b] pairs>'; prints the sign of
// compareTime(a, b) for each pair.
import { compareTime } from "../../web/dist/facts.js";

const pairs = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(pairs.map(([a, b]) => Math.sign(compareTime(a, b)))));
