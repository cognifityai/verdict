const SECTIONS = {
  overview: new Set(["summary", "reliability", "performance", "behavior"]),
  explore: new Set(["runs", "calls", "compare"]),
  evaluate: new Set(["results", "lab", "inspect", "review"]),
  monitor: new Set(["status", "history", "segments", "schedule"]),
  report: new Set(["management"]),
  settings: new Set(["sources", "alerts", "integrations", "privacy"]),
};

const DEFAULT_SECTION = {
  overview: "summary", explore: "runs", evaluate: "results",
  monitor: "status", report: "management", settings: "sources",
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

function selectionId(value) {
  return typeof value === "string" && !value.includes("\0")
    ? bounded(value, 256) : null;
}

function exactSelectionParams(params, allowed) {
  const keys = [...params.keys()];
  return keys.every((key) => allowed.has(key))
    && [...allowed].every((key) => key === "event_id" || params.getAll(key).length === 1)
    && keys.every((key) => params.getAll(key).length === 1);
}

export function parseDashboardSelection(search) {
  if (typeof search !== "string" || search === "" || search === "?") {
    return { state: "none", route: null };
  }
  const source = search.replace(/^\?/, "");
  try {
    decodeURIComponent(source.replaceAll("+", "%20"));
  } catch {
    return { state: "invalid", route: null };
  }
  const params = new URLSearchParams(source);
  const view = params.get("view");
  if (view === "traces"
      && exactSelectionParams(params, new Set(["view", "trace_id"]))) {
    const traceId = selectionId(params.get("trace_id"));
    if (traceId) return {
      state: "valid",
      route: {
        tab: "explore", section: "calls", explicit: true,
        findingCode: null, runIds: [], selectedRunId: null,
        runIdsTruncated: false, traceJudgeStatus: "all",
        traceId, evaluatorId: null,
      },
    };
  }
  if (view === "agent-runs"
      && exactSelectionParams(params, new Set(["view", "run_id", "event_id"]))) {
    const runId = selectionId(params.get("run_id"));
    const eventValue = params.get("event_id");
    const eventId = eventValue === null ? null : selectionId(eventValue);
    if (runId && (eventValue === null || eventId)) return {
      state: "valid",
      route: {
        tab: "explore", section: "runs", explicit: true,
        findingCode: null, runIds: [runId], selectedRunId: runId,
        runIdsTruncated: false, traceJudgeStatus: "all",
        traceId: null, evaluatorId: null, eventId,
      },
    };
  }
  return { state: "invalid", route: null };
}

function normalizedDestination(params, fallbackTab) {
  const requested = params.get("tab");
  if (requested === "monitor" && params.get("section") === "signals") {
    return ["monitor", "history"];
  }
  if (SECTIONS[requested]) {
    const section = params.get("section");
    return [requested, SECTIONS[requested].has(section) ? section : DEFAULT_SECTION[requested]];
  }
  if (requested === "drift") {
    const drift = params.get("drift");
    if (drift === "clusters") return ["monitor", "segments"];
    if (drift === "explore") return ["monitor", "history"];
    return ["monitor", "history"];
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
  const retiredDriftRoute = requestedTab === "drift"
    || (requestedTab === "monitor" && params.get("section") === "signals");
  const [tab, section] = normalizedDestination(params, fallbackTab);
  const runIds = [...new Set(params.getAll("run")
    .map((value) => bounded(value, 256)).filter(Boolean))].slice(0, 50);
  const selected = bounded(params.get("selected"), 256);
  const selectedRunId = selected && runIds.includes(selected) ? selected : runIds[0] || null;
  const requestedJudgeStatus = params.get("judge");
  const traceJudgeStatus = ["all", "judged", "not_judged", "judge_error", "pass", "fail", "unclear"]
    .includes(requestedJudgeStatus) ? requestedJudgeStatus : "all";
  return {
    tab,
    section,
    explicit: Boolean(SECTIONS[requestedTab] || LEGACY_ROUTES[requestedTab] || requestedTab === "drift"),
    findingCode: bounded(params.get("finding"), 128),
    runIds,
    selectedRunId,
    runIdsTruncated: params.get("truncated") === "1",
    traceJudgeStatus,
    traceId: bounded(params.get("trace"), 256),
    evaluatorId: retiredDriftRoute ? null : bounded(params.get("evaluator"), 64),
    eventId: tab === "explore" && section === "runs" && selectedRunId
      ? selectionId(params.get("event_id")) : null,
  };
}

export function canonicalDashboardHash(hash, fallbackTab = "overview") {
  const source = typeof hash === "string" ? hash.replace(/^#\??/, "") : "";
  const params = new URLSearchParams(source);
  const requested = params.get("tab");
  if (SECTIONS[requested]?.has(params.get("section"))) return null;
  if (requested === "monitor" && params.get("section") === "signals") {
    return serializeDashboardRoute(parseDashboardRoute(hash, fallbackTab));
  }
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
  if (tab === "explore" && section === "runs" && runIds.includes(route?.selectedRunId)) {
    const eventId = selectionId(route?.eventId);
    if (eventId) params.set("event_id", eventId);
  }
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
