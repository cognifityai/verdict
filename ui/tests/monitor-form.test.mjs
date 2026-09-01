import assert from "node:assert/strict";
import test from "node:test";

import { monitorRequest } from "../monitor-form.mjs";

test("count windows preserve numeric policy fields", () => {
  const form = { windowMode: "count", referenceRatio: 0.8, referenceStart: "" };

  assert.deepEqual(monitorRequest(form), form);
  assert.notEqual(monitorRequest(form), form);
});

test("explicit local datetime values are converted only at the request boundary", () => {
  const form = {
    windowMode: "explicit",
    referenceStart: "2026-08-01T12:30",
    referenceEnd: "2026-08-02T12:30",
    currentStart: "2026-08-03T12:30",
    currentEnd: "2026-08-04T12:30",
  };

  const payload = monitorRequest(form);

  assert.equal(form.referenceStart, "2026-08-01T12:30");
  for (const name of ["referenceStart", "referenceEnd", "currentStart", "currentEnd"]) {
    assert.match(payload[name], /^2026-08-0[1-4]T\d\d:30:00\.000Z$/);
  }
});
