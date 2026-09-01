const TABS = new Set([
  "setup", "insights", "overview", "reliability", "performance", "behavior",
  "runs", "drift", "traces", "judge", "evaluators", "control", "compare",
  "operations",
]);

function bounded(value, maximum) {
  return typeof value === "string" && value.length > 0 && new TextEncoder().encode(value).length <= maximum
    ? value : null;
}

export function parseDashboardRoute(hash, fallbackTab = "overview") {
  const source = typeof hash === "string" ? hash.replace(/^#\??/, "") : "";
  const params = new URLSearchParams(source);
  const requestedTab = params.get("tab");
  const tab = TABS.has(requestedTab) ? requestedTab : fallbackTab;
  const runIds = [...new Set(params.getAll("run").map((value) => bounded(value, 256)).filter(Boolean))].slice(0, 50);
  const selected = bounded(params.get("selected"), 256);
  const requestedDriftSection = params.get("drift");
  const driftSection = ["overview", "explore", "monitor", "signals", "clusters"]
    .includes(requestedDriftSection) ? requestedDriftSection : "overview";
  const requestedJudgeStatus = params.get("judge");
  const traceJudgeStatus = ["all", "judged", "not_judged", "judge_error", "pass", "fail", "unclear"]
    .includes(requestedJudgeStatus) ? requestedJudgeStatus : "all";
  return {
    tab,
    explicit: TABS.has(requestedTab),
    findingCode: bounded(params.get("finding"), 128),
    runIds,
    selectedRunId: selected && runIds.includes(selected) ? selected : runIds[0] || null,
    runIdsTruncated: params.get("truncated") === "1",
    driftSection,
    traceJudgeStatus,
    traceId: bounded(params.get("trace"), 256),
    evaluatorId: bounded(params.get("evaluator"), 64),
  };
}

export function serializeDashboardRoute(route) {
  const params = new URLSearchParams();
  params.set("tab", TABS.has(route?.tab) ? route.tab : "overview");
  const findingCode = bounded(route?.findingCode, 128);
  if (findingCode) params.set("finding", findingCode);
  const runIds = [...new Set(Array.isArray(route?.runIds) ? route.runIds : [])]
    .map((value) => bounded(value, 256)).filter(Boolean).slice(0, 50);
  for (const runId of runIds) params.append("run", runId);
  if (runIds.includes(route?.selectedRunId)) params.set("selected", route.selectedRunId);
  if (route?.runIdsTruncated === true) params.set("truncated", "1");
  if (route?.tab === "drift" && ["overview", "explore", "monitor", "signals", "clusters"].includes(route?.driftSection)) {
    params.set("drift", route.driftSection);
  }
  if (route?.tab === "traces") {
    if (["judged", "not_judged", "judge_error", "pass", "fail", "unclear"].includes(route?.traceJudgeStatus)) params.set("judge", route.traceJudgeStatus);
    const traceId = bounded(route?.traceId, 256);
    if (traceId) params.set("trace", traceId);
  }
  const evaluatorId = bounded(route?.evaluatorId, 64);
  if (evaluatorId) params.set("evaluator", evaluatorId);
  return `#${params.toString()}`;
}
