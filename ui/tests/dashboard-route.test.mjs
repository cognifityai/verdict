import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalDashboardHash, parseDashboardRoute, parseDashboardSelection,
  serializeDashboardRoute,
} from "../dashboard-route.mjs";

test("finding routes preserve every bounded affected run and the selected run", () => {
  const hash = serializeDashboardRoute({
    tab: "explore", section: "runs", findingCode: "tool_error", runIds: ["run-40", "run-2"],
    selectedRunId: "run-2", runIdsTruncated: true,
  });
  assert.deepEqual(parseDashboardRoute(hash), {
    tab: "explore", section: "runs", explicit: true, findingCode: "tool_error",
    runIds: ["run-40", "run-2"], selectedRunId: "run-2", runIdsTruncated: true,
    traceJudgeStatus: "all", traceId: null, evaluatorId: null, eventId: null,
  });
});

test("invalid direct-link state fails closed without preserving unrelated runs", () => {
  assert.deepEqual(parseDashboardRoute("#tab=nope&run=&selected=secret", "insights"), {
    tab: "overview", section: "summary", explicit: false, findingCode: null, runIds: [],
    selectedRunId: null, runIdsTruncated: false,
    traceJudgeStatus: "all", traceId: null, evaluatorId: null, eventId: null,
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

test("fixed-window signals have a refreshable route and legacy drift links open it", () => {
  const direct = parseDashboardRoute("#tab=monitor&section=signals&evaluator=evaluator-2");
  assert.equal(direct.tab, "monitor");
  assert.equal(direct.section, "signals");
  assert.equal(direct.evaluatorId, "evaluator-2");
  assert.equal(serializeDashboardRoute(direct), "#tab=monitor&section=signals&evaluator=evaluator-2");

  const legacy = parseDashboardRoute("#tab=drift&evaluator=evaluator-2");
  assert.equal(legacy.tab, "monitor");
  assert.equal(legacy.section, "signals");
  assert.equal(canonicalDashboardHash("#tab=drift&evaluator=evaluator-2"),
    "#tab=monitor&section=signals&evaluator=evaluator-2");
});

test("Inspect JSON is a stable Evaluate route", () => {
  const route = parseDashboardRoute("#tab=evaluate&section=inspect");
  assert.equal(route.tab, "evaluate");
  assert.equal(route.section, "inspect");
  assert.equal(serializeDashboardRoute(route), "#tab=evaluate&section=inspect");
});

test("Management Report is a stable top-level route", () => {
  const route = parseDashboardRoute("#tab=report&section=management");
  assert.equal(route.tab, "report");
  assert.equal(route.section, "management");
  assert.equal(serializeDashboardRoute(route), "#tab=report&section=management");
});

test("legacy dashboard destinations redirect into the current workspaces", () => {
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

test("public trace and agent links select only their exact bounded destination", () => {
  assert.deepEqual(
    parseDashboardSelection("?view=traces&trace_id=trace%2Fone"),
    {
      state: "valid",
      route: {
        tab: "explore", section: "calls", explicit: true,
        findingCode: null, runIds: [], selectedRunId: null,
        runIdsTruncated: false, traceJudgeStatus: "all",
        traceId: "trace/one", evaluatorId: null,
      },
    },
  );
  assert.deepEqual(
    parseDashboardSelection("?view=agent-runs&run_id=run-one&event_id=event-one"),
    {
      state: "valid",
      route: {
        tab: "explore", section: "runs", explicit: true,
        findingCode: null, runIds: ["run-one"], selectedRunId: "run-one",
        runIdsTruncated: false, traceJudgeStatus: "all",
        traceId: null, evaluatorId: null, eventId: "event-one",
      },
    },
  );
});

test("public Agent-event selection survives canonical hash serialization", () => {
  const selection = parseDashboardSelection(
    "?view=agent-runs&run_id=run-one&event_id=event-one",
  );
  assert.equal(selection.state, "valid");
  const hash = serializeDashboardRoute(selection.route);
  assert.equal(
    hash,
    "#tab=explore&section=runs&run=run-one&selected=run-one&event_id=event-one",
  );
  const restored = parseDashboardRoute(hash);
  assert.equal(restored.selectedRunId, "run-one");
  assert.equal(restored.eventId, "event-one");
});

test("event focus is dropped outside one selected Agent Run", () => {
  for (const route of [
    { tab: "explore", section: "runs", eventId: "event-one" },
    {
      tab: "explore", section: "runs", runIds: ["run-one"],
      selectedRunId: "run-one", eventId: "bad\0event",
    },
    {
      tab: "explore", section: "calls", runIds: ["run-one"],
      selectedRunId: "run-one", eventId: "event-one",
    },
  ]) {
    assert.equal(serializeDashboardRoute(route).includes("event_id="), false);
  }
});

test("unknown duplicate malformed and oversized public selections fail closed", () => {
  assert.deepEqual(parseDashboardSelection(""), { state: "none", route: null });
  for (const search of [
    "?view=traces",
    "?view=traces&trace_id=one&trace_id=two",
    "?view=traces&trace_id=one&secret=two",
    "?view=agent-runs&event_id=event-one",
    "?view=agent-runs&run_id=run-one&event_id=event-one&event_id=event-two",
    "?view=unknown&trace_id=trace-one",
    "?view=traces&trace_id=%E0%A4%A",
    `?view=traces&trace_id=${"x".repeat(257)}`,
    "?view=traces&trace_id=%00",
  ]) {
    assert.deepEqual(parseDashboardSelection(search), { state: "invalid", route: null }, search);
  }
});
