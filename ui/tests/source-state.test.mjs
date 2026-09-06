import assert from "node:assert/strict";
import test from "node:test";

import {
  initialDashboardTab,
  setupFailureMessage,
} from "../source-state.mjs";

test("agent runs open the findings-first overview", () => {
  const meta = { totalTraces: 0, totalAgentRuns: 55 };
  assert.equal(initialDashboardTab(meta), "overview");
});

test("trace-only and empty stores retain their distinct first destinations", () => {
  assert.equal(initialDashboardTab({ totalTraces: 1, totalAgentRuns: 0 }), "overview");
  assert.equal(initialDashboardTab({ totalTraces: 0, totalAgentRuns: 0 }), "settings");
});

test("setup network failures identify the unreachable Verdict origin", () => {
  assert.equal(
    setupFailureMessage(new TypeError("Failed to fetch"), "http://127.0.0.1:18901"),
    "Cannot reach the Verdict server at http://127.0.0.1:18901. Restart that server and reload this page.",
  );
  assert.equal(setupFailureMessage(new Error("HTTP 403"), "http://local"), "Error: HTTP 403");
});
