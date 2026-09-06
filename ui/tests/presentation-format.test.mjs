import assert from "node:assert/strict";
import test from "node:test";

import { formatCaptureRange, formatLatency, timelineTick } from "../presentation-format.mjs";

test("historical chart ticks use event dates instead of elapsed-hour labels", () => {
  assert.equal(timelineTick("2000-01-01T00:00:00Z", 227928), "Jan 1, 2026 00:00");
});

test("subsecond latency remains visible", () => {
  assert.equal(formatLatency(0.001), "1 ms");
  assert.equal(formatLatency(null), "Unavailable");
});

test("capture range names both event dates", () => {
  assert.equal(
    formatCaptureRange("2026-01-27T00:00:00Z", "2026-04-15T00:00:00Z", 1872),
    "Jan 27, 2026 – Apr 15, 2026",
  );
});
