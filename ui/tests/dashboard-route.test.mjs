import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalDashboardHash, parseDashboardRoute, serializeDashboardRoute,
} from "../dashboard-route.mjs";

test("finding routes preserve every bounded affected run and the selected run", () => {
  const hash = serializeDashboardRoute({
    tab: "explore", section: "runs", findingCode: "tool_error", runIds: ["run-40", "run-2"],
    selectedRunId: "run-2", runIdsTruncated: true,
  });
  assert.deepEqual(parseDashboardRoute(hash), {
    tab: "explore", section: "runs", explicit: true, findingCode: "tool_error",
    runIds: ["run-40", "run-2"], selectedRunId: "run-2", runIdsTruncated: true,
    traceJudgeStatus: "all", traceId: null, evaluatorId: null,
  });
});

test("invalid direct-link state fails closed without preserving unrelated runs", () => {
  assert.deepEqual(parseDashboardRoute("#tab=nope&run=&selected=secret", "insights"), {
    tab: "overview", section: "summary", explicit: false, findingCode: null, runIds: [],
    selectedRunId: null, runIdsTruncated: false,
    traceJudgeStatus: "all", traceId: null, evaluatorId: null,
  });
});

test("trace evaluator filters and exact trace links round trip", () => {
  const route = parseDashboardRoute(serializeDashboardRoute({
    tab: "explore", section: "calls", traceJudgeStatus: "judge_error", traceId: "trace-900",
    evaluatorId: "evaluator-2",
  }));
  assert.equal(route.traceJudgeStatus, "judge_error");
  assert.equal(route.traceId, "trace-900");
  assert.equal(route.evaluatorId, "evaluator-2");
});

test("drift subsection survives refreshable direct links", () => {
  assert.equal(
    parseDashboardRoute("#tab=drift&drift=clusters").tab,
    "monitor",
  );
  assert.equal(parseDashboardRoute("#tab=drift&drift=clusters").section, "segments");
});

test("legacy dashboard destinations redirect into five coherent workspaces", () => {
  const cases = [
    ["#tab=reliability", "overview", "reliability"],
    ["#tab=performance", "overview", "performance"],
    ["#tab=behavior", "overview", "behavior"],
    ["#tab=runs", "explore", "runs"],
    ["#tab=traces&trace=trace-1", "explore", "calls"],
    ["#tab=judge", "evaluate", "results"],
    ["#tab=evaluators", "evaluate", "lab"],
    ["#tab=control", "settings", "alerts"],
    ["#tab=compare", "explore", "compare"],
    ["#tab=setup", "settings", "sources"],
  ];
  for (const [hash, tab, section] of cases) {
    const route = parseDashboardRoute(hash);
    assert.equal(route.tab, tab, hash);
    assert.equal(route.section, section, hash);
    assert.equal(
      canonicalDashboardHash(hash),
      serializeDashboardRoute(route),
      hash,
    );
  }
  assert.equal(canonicalDashboardHash("#tab=overview&section=summary"), null);
});
