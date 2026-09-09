import assert from "node:assert/strict";
import test from "node:test";

import {
  observedSourcePresentation,
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

test("observed sources distinguish local, SDK, trace-only, and mixed telemetry", () => {
  const local = [{ sourceKind: "claude-code", runs: 1 }, { sourceKind: "codex", runs: 1 }];
  const mixed = [...local.slice(0, 1), { sourceKind: "verdict_sdk", runs: 1 }, { sourceKind: "custom-adapter", runs: 1 }];
  const cases = [
    [{ totalAgentRuns: 4, agentRunSources: [{ sourceKind: "verdict_sdk", runs: 4 }] }, ["Instrumented agent telemetry", "Agent sources", false, true]],
    [{ totalAgentRuns: 2, agentRunSources: local }, ["Claude Code / Codex", "Local sources", true, false]],
    [{ totalAgentRuns: 0, totalTraces: 3 }, ["LLM telemetry", "Source type", false, false]],
    [{ totalAgentRuns: 1, agentRunSources: [{ sourceKind: "custom-adapter", runs: 1 }] }, ["Agent telemetry", "Agent sources", false, false]],
    [{ totalAgentRuns: 3, totalTraces: 3, agentRunSources: mixed }, ["Agent and LLM telemetry", "Agent sources", true, true]],
    [{ totalAgentRuns: 4, agentRunSources: [{ sourceKind: "verdict_sdk", runs: 3 }], agentRunSourcesTruncated: true }, ["Agent telemetry", "Agent sources", false, true]],
    [{ totalAgentRuns: 2, agentRunSources: [{ sourceKind: "verdict_sdk", runs: 1 }, { sourceKind: "verdict_sdk", runs: 1 }] }, ["Agent telemetry", "Agent sources", false, true]],
  ];

  for (const [meta, expected] of cases) {
    const result = observedSourcePresentation(meta);
    assert.deepEqual(
      [result.heading, result.sourceLabel, result.hasLocalAgents, result.hasSdkAgents],
      expected,
    );
  }
});
