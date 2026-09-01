import assert from "node:assert/strict";
import test from "node:test";

import { parseDashboardRoute, serializeDashboardRoute } from "../dashboard-route.mjs";

test("finding routes preserve every bounded affected run and the selected run", () => {
  const hash = serializeDashboardRoute({
    tab: "runs", findingCode: "tool_error", runIds: ["run-40", "run-2"],
    selectedRunId: "run-2", runIdsTruncated: true,
  });
  assert.deepEqual(parseDashboardRoute(hash), {
    tab: "runs", explicit: true, findingCode: "tool_error",
    runIds: ["run-40", "run-2"], selectedRunId: "run-2", runIdsTruncated: true,
    driftSection: "overview", traceJudgeStatus: "all", traceId: null, evaluatorId: null,
  });
});

test("invalid direct-link state fails closed without preserving unrelated runs", () => {
  assert.deepEqual(parseDashboardRoute("#tab=nope&run=&selected=secret", "insights"), {
    tab: "insights", explicit: false, findingCode: null, runIds: [],
    selectedRunId: null, runIdsTruncated: false, driftSection: "overview",
    traceJudgeStatus: "all", traceId: null, evaluatorId: null,
  });
});

test("trace evaluator filters and exact trace links round trip", () => {
  const route = parseDashboardRoute(serializeDashboardRoute({
    tab: "traces", traceJudgeStatus: "judge_error", traceId: "trace-900",
    evaluatorId: "evaluator-2",
  }));
  assert.equal(route.traceJudgeStatus, "judge_error");
  assert.equal(route.traceId, "trace-900");
  assert.equal(route.evaluatorId, "evaluator-2");
});

test("drift subsection survives refreshable direct links", () => {
  assert.equal(
    parseDashboardRoute(serializeDashboardRoute({ tab: "drift", driftSection: "clusters" })).driftSection,
    "clusters",
  );
});
