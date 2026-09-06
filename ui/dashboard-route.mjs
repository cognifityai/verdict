const SECTIONS = {
  overview: new Set(["summary", "reliability", "performance", "behavior"]),
  explore: new Set(["runs", "calls", "compare"]),
  evaluate: new Set(["results", "lab", "review"]),
  monitor: new Set(["status", "history", "segments", "schedule"]),
  settings: new Set(["sources", "alerts", "integrations", "privacy"]),
};

const DEFAULT_SECTION = {
  overview: "summary", explore: "runs", evaluate: "results",
  monitor: "status", settings: "sources",
};

const LEGACY_ROUTES = {
  setup: ["settings", "sources"],
  insights: ["overview", "summary"],
  overview: ["overview", "summary"],
  reliability: ["overview", "reliability"],
  performance: ["overview", "performance"],
  behavior: ["overview", "behavior"],
  runs: ["explore", "runs"],
  traces: ["explore", "calls"],
  judge: ["evaluate", "results"],
  evaluators: ["evaluate", "lab"],
  control: ["settings", "alerts"],
  compare: ["explore", "compare"],
  operations: ["settings", "integrations"],
};

function bounded(value, maximum) {
  return typeof value === "string" && value.length > 0
    && new TextEncoder().encode(value).length <= maximum ? value : null;
}

function normalizedDestination(params, fallbackTab) {
  const requested = params.get("tab");
  if (SECTIONS[requested]) {
    const section = params.get("section");
    return [requested, SECTIONS[requested].has(section) ? section : DEFAULT_SECTION[requested]];
  }
  if (requested === "drift") {
    const drift = params.get("drift");
    if (drift === "clusters") return ["monitor", "segments"];
    if (drift === "explore") return ["monitor", "history"];
    return ["monitor", "status"];
  }
  if (LEGACY_ROUTES[requested]) return LEGACY_ROUTES[requested];
  if (SECTIONS[fallbackTab]) return [fallbackTab, DEFAULT_SECTION[fallbackTab]];
  if (LEGACY_ROUTES[fallbackTab]) return LEGACY_ROUTES[fallbackTab];
  return ["overview", "summary"];
}

export function parseDashboardRoute(hash, fallbackTab = "overview") {
  const source = typeof hash === "string" ? hash.replace(/^#\??/, "") : "";
  const params = new URLSearchParams(source);
  const requestedTab = params.get("tab");
  const [tab, section] = normalizedDestination(params, fallbackTab);
  const runIds = [...new Set(params.getAll("run")
    .map((value) => bounded(value, 256)).filter(Boolean))].slice(0, 50);
  const selected = bounded(params.get("selected"), 256);
  const requestedJudgeStatus = params.get("judge");
  const traceJudgeStatus = ["all", "judged", "not_judged", "judge_error", "pass", "fail", "unclear"]
    .includes(requestedJudgeStatus) ? requestedJudgeStatus : "all";
  return {
    tab,
    section,
    explicit: Boolean(SECTIONS[requestedTab] || LEGACY_ROUTES[requestedTab] || requestedTab === "drift"),
    findingCode: bounded(params.get("finding"), 128),
    runIds,
    selectedRunId: selected && runIds.includes(selected) ? selected : runIds[0] || null,
    runIdsTruncated: params.get("truncated") === "1",
    traceJudgeStatus,
    traceId: bounded(params.get("trace"), 256),
    evaluatorId: bounded(params.get("evaluator"), 64),
  };
}

export function canonicalDashboardHash(hash, fallbackTab = "overview") {
  const source = typeof hash === "string" ? hash.replace(/^#\??/, "") : "";
  const params = new URLSearchParams(source);
  const requested = params.get("tab");
  if (SECTIONS[requested]?.has(params.get("section"))) return null;
  if (requested !== "drift" && !LEGACY_ROUTES[requested]) return null;
  return serializeDashboardRoute(parseDashboardRoute(hash, fallbackTab));
}

export function serializeDashboardRoute(route) {
  const tab = SECTIONS[route?.tab] ? route.tab : "overview";
  const section = SECTIONS[tab].has(route?.section) ? route.section : DEFAULT_SECTION[tab];
  const params = new URLSearchParams();
  params.set("tab", tab);
  params.set("section", section);
  const findingCode = bounded(route?.findingCode, 128);
  if (findingCode) params.set("finding", findingCode);
  const runIds = [...new Set(Array.isArray(route?.runIds) ? route.runIds : [])]
    .map((value) => bounded(value, 256)).filter(Boolean).slice(0, 50);
  for (const runId of runIds) params.append("run", runId);
  if (runIds.includes(route?.selectedRunId)) params.set("selected", route.selectedRunId);
  if (route?.runIdsTruncated === true) params.set("truncated", "1");
  if (tab === "explore" && section === "calls") {
    if (["judged", "not_judged", "judge_error", "pass", "fail", "unclear"].includes(route?.traceJudgeStatus)) {
      params.set("judge", route.traceJudgeStatus);
    }
    const traceId = bounded(route?.traceId, 256);
    if (traceId) params.set("trace", traceId);
  }
  const evaluatorId = bounded(route?.evaluatorId, 64);
  if (evaluatorId) params.set("evaluator", evaluatorId);
  return `#${params.toString()}`;
}
