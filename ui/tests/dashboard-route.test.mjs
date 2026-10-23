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

test("recorded source and agent group routes filter runs without carrying an old selection", () => {
  for (const [field, parameter, value] of [
    ["sourceKind", "source", "custom/agent"],
    ["agentName", "agent", "research worker"],
  ]) {
    const hash = serializeDashboardRoute({ tab: "explore", section: "runs", [field]: value,
      runIds: [], selectedRunId: "old-run", eventId: "old-event" });
    assert.match(hash, new RegExp(`${parameter}=`));
    const parsed = parseDashboardRoute(hash);
    assert.equal(parsed[field], value);
    assert.equal(parsed.selectedRunId, null);
    assert.equal(parsed.eventId, null);
  }
  assert.equal(parseDashboardRoute(`#tab=explore&section=runs&source=${"x".repeat(257)}`).sourceKind, undefined);
  const combined = parseDashboardRoute(serializeDashboardRoute({ tab: "explore", section: "runs",
    sourceKind: "codex", agentName: "writer", runIds: [], selectedRunId: "old-run" }));
  assert.equal(combined.sourceKind, "codex");
  assert.equal(combined.agentName, "writer");
  assert.equal(combined.selectedRunId, null);
  assert.doesNotMatch(serializeDashboardRoute({ tab: "monitor", section: "status", sourceKind: "codex" }), /source=/);
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

test("UTC model-call date survives trace route selection and rejects impossible dates", () => {
  const route = parseDashboardRoute(serializeDashboardRoute({
    tab: "explore", section: "calls", traceDate: "2026-10-07", traceJudgeStatus: "fail",
    traceId: "trace-7",
  }));
  assert.equal(route.traceDate, "2026-10-07");
  assert.equal(route.traceId, "trace-7");
  assert.equal(route.traceJudgeStatus, "fail");
  assert.equal(parseDashboardRoute("#tab=explore&section=calls&date=2026-02-30").traceDate ?? null, null);
  assert.equal(parseDashboardRoute("#tab=explore&section=runs&date=2026-10-07").traceDate ?? null, null);
});

test("drift subsection survives refreshable direct links", () => {
  assert.equal(
    parseDashboardRoute("#tab=drift&drift=clusters").tab,
    "monitor",
  );
  assert.equal(parseDashboardRoute("#tab=drift&drift=clusters").section, "segments");
});

test("Monitor opens its operational status before the setup and advanced views", () => {
  assert.equal(parseDashboardRoute("#tab=monitor").section, "status");
  assert.equal(parseDashboardRoute("#tab=monitor&section=plan").section, "plan");
  assert.equal(parseDashboardRoute("#tab=monitor&section=history").section, "history");
});

test("retired signals and drift links redirect to Monitor History", () => {
  const direct = parseDashboardRoute("#tab=monitor&section=signals&evaluator=evaluator-2");
  assert.equal(direct.tab, "monitor");
  assert.equal(direct.section, "history");
  assert.equal(direct.evaluatorId, null);
  assert.equal(canonicalDashboardHash("#tab=monitor&section=signals&evaluator=evaluator-2"),
    "#tab=monitor&section=history");

  const legacy = parseDashboardRoute("#tab=drift&evaluator=evaluator-2");
  assert.equal(legacy.tab, "monitor");
  assert.equal(legacy.section, "history");
  assert.equal(canonicalDashboardHash("#tab=drift&evaluator=evaluator-2"),
    "#tab=monitor&section=history");
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

test("exact notice context survives a selected run hash and clears outside runs", () => {
  const route = { tab: "explore", section: "runs", runIds: ["failed-run"],
    selectedRunId: "failed-run", eventId: "failed-event", noticeId: "notice-one" };
  const hash = serializeDashboardRoute(route);
  assert.equal(parseDashboardRoute(hash).noticeId, "notice-one");
  assert.equal(serializeDashboardRoute({ ...route, tab: "monitor", section: "status" }).includes("notice="), false);
  assert.equal(serializeDashboardRoute({ ...route, runIds: [], selectedRunId: null }).includes("notice="), false);
  assert.equal(serializeDashboardRoute({ ...route, noticeId: "bad\0notice" }).includes("notice="), false);
});

test("exact recent event links retain revision and original IDs only for a selected run", () => {
  const route = { tab: "explore", section: "runs", runIds: ["large-run"],
    selectedRunId: "large-run", recentRevision: "a".repeat(64),
    recentTurnId: "turn-original", recentEventId: "event-original" };
  const hash = serializeDashboardRoute(route);
  assert.match(hash, /recent_rev=/);
  const restored = parseDashboardRoute(hash);
  assert.equal(restored.recentRevision, route.recentRevision);
  assert.equal(restored.recentTurnId, route.recentTurnId);
  assert.equal(restored.recentEventId, route.recentEventId);
  assert.doesNotMatch(serializeDashboardRoute({ ...route, runIds: [], selectedRunId: null }), /recent_/);
  assert.doesNotMatch(serializeDashboardRoute({ ...route, recentRevision: "bad" }), /recent_/);
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
