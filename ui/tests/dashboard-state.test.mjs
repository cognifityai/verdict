import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { build } from "esbuild";

const UI_SOURCE = new URL("../VerdictUI.jsx", import.meta.url);

function reactStub() {
  return `
    const hooks = () => globalThis.__VERDICT_TEST_HOOKS__;
    const React = {
      createElement: (...args) => hooks().createElement(...args),
      useState: (...args) => hooks().useState(...args),
      useEffect: (...args) => hooks().useEffect(...args),
      useRef: (...args) => hooks().useRef(...args),
      useCallback: (...args) => hooks().useCallback(...args),
    };
    export default React;
    export const useState = (...args) => hooks().useState(...args);
    export const useEffect = (...args) => hooks().useEffect(...args);
    export const useRef = (...args) => hooks().useRef(...args);
  `;
}

function componentStub(names) {
  return names.map((name) => `export const ${name} = () => null;`).join("\n");
}

async function loadUiModule() {
  const source = `${await readFile(UI_SOURCE, "utf8")}\nexport { Dashboard, Overview, Traces, TraceDetail, Drift, Judge, Compare, mountedApiUrl };\nexport { useOperations } from "./Operations.jsx";\nexport { RegistryView } from "./Registry.jsx";`;
  const result = await build({
    stdin: {
      contents: source,
      loader: "jsx",
      resolveDir: new URL(".", UI_SOURCE).pathname,
      sourcefile: "VerdictUI.test.jsx",
    },
    bundle: true,
    format: "esm",
    platform: "node",
    write: false,
    plugins: [{
      name: "dashboard-test-stubs",
      setup(pluginBuild) {
        pluginBuild.onResolve({ filter: /^react$/ }, () => ({
          path: "react",
          namespace: "test-stub",
        }));
        pluginBuild.onResolve({ filter: /^recharts$/ }, () => ({
          path: "recharts",
          namespace: "test-stub",
        }));
        pluginBuild.onResolve({ filter: /^lucide-react$/ }, () => ({
          path: "lucide-react",
          namespace: "test-stub",
        }));
        pluginBuild.onLoad({ filter: /.*/, namespace: "test-stub" }, (args) => {
          if (args.path === "react") return { contents: reactStub(), loader: "js" };
          if (args.path === "recharts") {
            return { contents: componentStub([
              "LineChart", "Line", "AreaChart", "Area", "BarChart", "Bar",
              "XAxis", "YAxis", "CartesianGrid", "Tooltip", "ResponsiveContainer",
              "ReferenceLine", "ReferenceArea", "Cell",
            ]), loader: "js" };
          }
          return { contents: componentStub([
            "Activity", "AlertTriangle", "ArrowRight", "ArrowLeft", "BarChart3",
            "Boxes", "CheckCircle2", "Clock", "Code2", "Database", "GitBranch",
            "Layers", "Scale", "Search", "Shield", "Signal", "TrendingDown",
            "TrendingUp", "Zap", "Github", "Terminal", "Gauge", "FlaskConical",
            "Cpu", "DollarSign", "Filter", "X", "Sparkles", "ChevronRight",
            "Eye", "Network", "RefreshCw",
          ]), loader: "js" };
        });
      },
    }],
  });
  const encoded = Buffer.from(result.outputFiles[0].text).toString("base64");
  return import(`data:text/javascript;base64,${encoded}#${Math.random()}`);
}

function createHooks() {
  const states = [];
  const refs = [];
  let cursor = 0;
  let refCursor = 0;
  return {
    begin() { cursor = 0; refCursor = 0; },
    createElement(type, props, ...children) {
      return { type, props: { ...(props || {}), children } };
    },
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) {
        states[index] = typeof initial === "function" ? initial() : initial;
      }
      const setState = (next) => {
        states[index] = typeof next === "function" ? next(states[index]) : next;
      };
      return [states[index], setState];
    },
    useEffect() {},
    useRef(initial) {
      const index = refCursor++;
      if (!(index in refs)) refs[index] = { current: initial };
      return refs[index];
    },
    useCallback(fn) { return fn; },
  };
}

function render(component, hooks, props = {}) {
  globalThis.__VERDICT_TEST_HOOKS__ = hooks;
  hooks.begin();
  return component(props);
}

function findAll(node, predicate, found = []) {
  if (node == null || typeof node === "boolean") return found;
  if (Array.isArray(node)) {
    for (const child of node) findAll(child, predicate, found);
    return found;
  }
  if (typeof node !== "object") return found;
  if (predicate(node)) found.push(node);
  findAll(node.props?.children, predicate, found);
  return found;
}

function textOf(node) {
  if (node == null || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(textOf).join(" ");
  if (typeof node !== "object") return String(node);
  return textOf(node.props?.children);
}

function dashboardElement(tree) {
  return findAll(
    tree,
    (node) => typeof node.type === "function" && node.type.name === "Dashboard",
  )[0];
}

function bundle(evaluator, samples = [], driftSignals = []) {
  return {
    meta: { totalTraces: samples.length, totalJudged: 0, workload: null },
    evaluation: { selectedId: evaluator, availableIdentities: [] },
    driftAnalysis: {
      runStatus: "no_completed_run", readinessStatus: "not_enough_current",
      current: 0, baseline: 0, minimum: 30,
      currentHours: 24, baselineLagHours: 24, baselineDays: 7,
    },
    driftRun: null,
    clusterHealth: { status: "empty", messages: [], minSampleSize: 30, clustersMeetingSampleFloor: 0, nClusters: 0 },
    providers: [], clusters: [], driftSignals, dimensionOverall: [], tsRows: [],
    passrate: [], clusterPassrate: [], haikuDim: [], samples,
    providerDimension: [], evaluatorHealth: [], scoreCoverage: {},
    truncation: {
      applied: samples.length > 30,
      resources: { traceSamples: { available: samples.length, shown: Math.min(samples.length, 30), limit: 30 } },
    },
  };
}

function deferredFetches() {
  const requests = [];
  globalThis.fetch = (url, options = {}) => new Promise((resolve, reject) => {
    requests.push({ url: String(url), options, resolve, reject });
  });
  return requests;
}

test("a mounted dashboard derives its API path from the host prefix", async () => {
  globalThis.window = { location: { pathname: "/admin/verdict/dashboard" } };
  try {
    const ui = await loadUiModule();
    assert.equal(ui.mountedApiUrl(), "/admin/verdict/api/data");
  } finally {
    delete globalThis.window;
  }
});

test("operations navigation is present only when the host configures an adapter", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();

  const withoutAdapter = render(ui.Dashboard, hooks, {
    data: bundle("judge-a"),
    operationsUrl: null,
  });
  assert.equal(textOf(withoutAdapter).includes("Operations"), false);

  const withAdapter = render(ui.Dashboard, hooks, {
    data: bundle("judge-a"),
    operationsUrl: "/api/admin/operations",
  });
  assert.equal(textOf(withAdapter).includes("Operations"), true);
});

test("registry view discloses experimental status, readiness, explanations, and actions", async () => {
  const ui = await loadUiModule();
  const actions = [];
  const tree = render(ui.RegistryView, createHooks(), {
    data: {
      tenant: "tenant-a",
      status: "ready",
      active: { versionId: "crv-old", generation: 3 },
      versions: [
        { versionId: "crv-old", strategy: "explicit", active: true },
        { versionId: "crv-preview", strategy: "hybrid", active: false },
      ],
      selectedVersion: {
        versionId: "crv-preview",
        strategy: "hybrid",
        active: false,
        strategyStatus: { experimental: true, semanticComponent: "fallback" },
        preview: { warnings: ["fit warning"] },
        configuration: {},
        algorithm: "ward-best-k-v2",
        selector: "latest-user-v1",
        model: { name: "MiniLM", revision: "frozen" },
      },
      readiness: { status: "validated", passed: true, coverage: true, structural: true, definition: true, model: true },
      activationHistory: true,
      counts: { assigned: 8, outlier: 1, ineligible: 1, total: 10 },
      modelDistribution: [{ provider: "anthropic", model: "claude", count: 8 }],
      trafficWindow: { conversationFloor: 30 },
      healthWarnings: ["fragmented_semantic_space"],
      clusters: [{ clusterId: "clu-a", displayName: "Billing", kind: "explicit", lifecycle: "provisional", explicitKey: "billing", assignedCount: 8, memberCount: 8, outlierCount: 1, radius: null, detailsAvailable: true, representatives: [{ traceId: "trace-a", prompt: "Redacted billing prompt", provider: "anthropic", model: "claude" }], modelDistribution: [{ provider: "anthropic", model: "claude", count: 8 }], conversationReadiness: { status: "collecting", floor: 30, baseline: 20, current: 8, remainingBaseline: 10, remainingCurrent: 22, estimatedDaysToReady: 3 }, warnings: [] }],
      assignments: [{ traceId: "trace-a", origin: "incremental", status: "ineligible", reason: "missing_intent_key" }],
      reasons: [{ status: "ineligible", reason: "missing_intent_key", count: 1 }],
      events: [{ action: "activated" }],
      page: { offset: 0, available: 1, shown: 1, truncated: false },
    },
    operations: { available: true, running: null, jobs: [] },
    onRun: (kind, parameters) => actions.push([kind, parameters]),
    onVersion: () => {},
    onPage: () => {},
  });
  const rendered = textOf(tree);

  assert.match(rendered, /Experimental opt-in/);
  assert.match(rendered, /30\.1047%/);
  assert.match(rendered, /Validated/);
  assert.match(rendered, /Redacted billing prompt/);
  assert.match(rendered, /Baseline\s+20\s+\/\s+30/);
  assert.match(rendered, /split across many small clusters/);
  assert.match(rendered, /ward-best-k-v2/);
  assert.match(rendered, /No explicit intent key was captured/);
  assert.match(rendered, /Activate version/);
  assert.match(rendered, /Refit active/);
  assert.match(rendered, /Rollback to version/);

  for (const label of ["Refit active", "Activate version", "Rollback to version"]) {
    findAll(
      tree,
      (node) => node.type?.name === "ActionButton" && textOf(node) === label,
    )[0].props.onClick();
  }
  assert.deepEqual(actions, [
    ["cluster_refit", {}],
    ["cluster_activate", { version: "crv-preview", expected_generation: 3 }],
    ["cluster_rollback", { version: "crv-preview", expected_generation: 3 }],
  ]);
});

test("the live dashboard does not display synthetic metrics before confirmation", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.DashboardRoot, createHooks());
  const dashboard = dashboardElement(tree);

  assert.equal(dashboard.props.source, "loading");
  assert.equal(dashboard.props.data.meta.totalTraces, 0);
  assert.equal(dashboard.props.data.samples.length, 0);
});

async function resolveJson(request, payload) {
  request.resolve({ ok: true, json: async () => payload });
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

test("an older evaluator response cannot overwrite the newest confirmed snapshot", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));

  dashboard.props.onEvaluatorChange("evaluator-a");
  dashboard.props.onEvaluatorChange("evaluator-b");
  assert.equal(requests.length, 2);

  await resolveJson(requests[1], bundle("evaluator-b"));
  await resolveJson(requests[0], bundle("evaluator-a"));

  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  dashboard.props.onReload();
  assert.match(requests[2].url, /evaluator=evaluator-b(?:&|$)/);
});

test("an older operations response cannot overwrite a newer refresh", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  const renderHook = () => {
    globalThis.__VERDICT_TEST_HOOKS__ = hooks;
    hooks.begin();
    return ui.useOperations("/api/admin/operations");
  };
  let state = renderHook();

  state.load();
  state.load();
  assert.equal(requests.length, 2);
  await resolveJson(requests[1], { generatedAt: "new", metrics: [] });
  await resolveJson(requests[0], { generatedAt: "old", metrics: [] });

  state = renderHook();
  assert.equal(state.data.generatedAt, "new");
});

test("a failed evaluator request names the snapshot that remains displayed", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  dashboard.props.onEvaluatorChange("evaluator-a");
  await resolveJson(requests[0], bundle("evaluator-a"));

  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  dashboard.props.onEvaluatorChange("evaluator-b");
  requests[1].reject(new Error("network unavailable"));
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));

  assert.equal(dashboard.props.data.evaluation.selectedId, "evaluator-a");
  assert.match(dashboard.props.loadError, /Still showing evaluator-a/);
});

test("trace detail is derived from the current snapshot instead of retaining an old object", async () => {
  const ui = await loadUiModule();
  const rootHooks = createHooks();
  const traceHooks = createHooks();
  const requests = deferredFetches();
  const oldData = bundle("evaluator-a", [{
    trace_id: "old-trace", provider: "openai", request_model: "model",
    prompt_redacted: "OLD_PROMPT", response_redacted: "OLD_RESPONSE",
    cluster_id: "support", hour: 1,
  }]);
  const newData = bundle("evaluator-b", [{
    trace_id: "new-trace", provider: "openai", request_model: "model",
    prompt_redacted: "NEW_PROMPT", response_redacted: "NEW_RESPONSE",
    cluster_id: "support", hour: 2,
  }]);

  let dashboard = dashboardElement(render(ui.DashboardRoot, rootHooks));
  dashboard.props.onEvaluatorChange("evaluator-a");
  await resolveJson(requests[0], oldData);

  let tree = render(ui.Traces, traceHooks, { data: oldData });
  const oldRow = findAll(
    tree,
    (node) => node.type === "button" && textOf(node).includes("OLD_PROMPT"),
  )[0];
  oldRow.props.onClick();
  tree = render(ui.Traces, traceHooks, { data: oldData });
  assert.equal(findAll(tree, (node) => node.type?.name === "TraceDetail")[0].props.s.trace_id, "old-trace");

  dashboard = dashboardElement(render(ui.DashboardRoot, rootHooks));
  dashboard.props.onEvaluatorChange("evaluator-b");
  await resolveJson(requests[1], newData);
  tree = render(ui.Traces, traceHooks, { data: newData });

  assert.equal(findAll(tree, (node) => node.type?.name === "TraceDetail").length, 0);
});

test("only one same-dimension drift card opens because state is keyed by signal id", async () => {
  const ui = await loadUiModule();
  const rootHooks = createHooks();
  const driftHooks = createHooks();
  const requests = deferredFetches();
  const data = bundle("evaluator-a", [], [
    { id: "signal-a", dimension: "quality", direction: "regression", layers: [], providerLabel: "A" },
    { id: "signal-b", dimension: "quality", direction: "regression", layers: [], providerLabel: "A" },
  ]);
  const dashboard = dashboardElement(render(ui.DashboardRoot, rootHooks));
  dashboard.props.onEvaluatorChange("evaluator-a");
  await resolveJson(requests[0], data);

  const tree = render(ui.Drift, driftHooks, { data });

  assert.equal((textOf(tree).match(/Recommended action/g) || []).length, 1);
});

test("drift chart renders custom dimensions and the runtime regression marker", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const data = bundle("evaluator-a");
  data.dimensionOverall = [{ dim: "action_correctness", passRate: 80 }];
  data.meta.regressionHour = 17;
  data.driftAnalysis.runStatus = "completed_no_signals";

  const tree = render(ui.Drift, hooks, { data });
  const lines = findAll(tree, (node) => node.type?.name === "Line");
  const areas = findAll(tree, (node) => node.type?.name === "ReferenceArea");

  assert.ok(lines.some((line) => line.props.dataKey === "action_correctness"));
  assert.ok(areas.some((area) => area.props.x1 === 17));
});

test("judge view renders the server's executable coverage snapshot", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const data = bundle("evaluator-a");
  data.scoreCoverage = {
    pass: 11, fail: 12, unclear: 13, missing: 14, error: 15, evaluable: 23,
  };

  const tree = render(ui.Judge, hooks, { data });
  const rendered = textOf(tree);

  for (const expected of ["PASS 11", "FAIL 12", "UNCLEAR 13", "Missing 14", "Errors 15", "Evaluable 23"]) {
    assert.match(rendered.replace(/\s+/g, " "), new RegExp(expected));
  }
});

test("dashboard visibly reports every bounded response resource", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const data = bundle("evaluator-a");
  data.truncation = {
    applied: true,
    resources: {
      latencyPoints: { available: 1000, shown: 100, limit: 100 },
      clusters: { available: 75, shown: 20, limit: 20 },
    },
  };

  const tree = render(ui.Dashboard, hooks, {
    data,
    source: "live",
    onReload() {},
    onEvaluatorChange() {},
    reloading: false,
    loadError: null,
  });
  const rendered = textOf(tree).replace(/\s+/g, " ");

  assert.match(rendered, /Showing a bounded dashboard view/);
  assert.match(rendered, /latency points: 100 of 1,000/);
  assert.match(rendered, /clusters: 20 of 75/);
});

test("live dashboard identity never falls back to the bundled sample service", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.meta.workload = "agent";

  const rendered = textOf(render(ui.Dashboard, createHooks(), {
    data,
    source: "live",
    onReload() {},
    onEvaluatorChange() {},
  }));

  assert.match(rendered, /Live Verdict store/);
  assert.match(rendered, /Workload: agent/);
  assert.doesNotMatch(rendered, /sample-service \/ local/i);
  assert.doesNotMatch(rendered, /WORKLOAD \/ SAMPLE-SERVICE/i);
});

test("live provider comparison never invents a regression badge", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.providers = [{
    key: "anthropic", label: "Anthropic Haiku", model: "claude-haiku-4-5",
    n: 3, errors: 0, errorRate: 0, avgLatency: 1.2, inTok: 10, outTok: 20,
    cost: 0.01, passRate: null, judged: 0,
  }];

  const tree = render(ui.Compare, createHooks(), { data, source: "live" });
  const rendered = textOf(tree).replace(/\s+/g, " ");

  assert.doesNotMatch(rendered, /regressed/);
  assert.doesNotMatch(rendered, /Bundled synthetic sample/);
});

test("live provider comparison shows only persisted provider regressions", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a", [], [
    { id: "signal-a", direction: "regression", provider: "anthropic" },
    { id: "signal-b", direction: "improvement", provider: "openai" },
  ]);
  data.providers = [
    {
      key: "anthropic", label: "Anthropic Haiku", model: "claude-haiku-4-5",
      n: 3, errors: 0, errorRate: 0, avgLatency: 1.2, inTok: 10, outTok: 20,
      cost: 0.01, passRate: 80, judged: 3,
    },
    {
      key: "openai", label: "OpenAI Nano", model: "gpt-test",
      n: 3, errors: 0, errorRate: 0, avgLatency: 1.1, inTok: 10, outTok: 20,
      cost: 0.01, passRate: 90, judged: 3,
    },
  ];

  const rendered = textOf(render(ui.Compare, createHooks(), { data, source: "live" }));

  assert.equal((rendered.match(/regressed/g) || []).length, 1);
});

test("metadata-only traces describe historical capture without claiming capture is off", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "metadata-trace", provider: "openai", request_model: "gpt-test",
    cluster_id: null, input_tokens: 12, output_tokens: 8, latency_ms: 150,
    cost_usd: null, error: null, hour: 0, prompt_redacted: null,
    response_redacted: null,
  };
  const data = bundle("evaluator-a", [sample]);
  data.providers = [{ key: "openai", rawProvider: "openai", model: "gpt-test" }];
  const tree = render(ui.Traces, createHooks(), { data });
  assert.match(textOf(tree), /Historical metadata-only trace/);
  assert.doesNotMatch(textOf(tree), /Content capture off/);

  const detail = render(ui.TraceDetail, createHooks(), { s: sample, onClose() {} });
  assert.match(textOf(detail), /Historical metadata-only trace/);
  assert.match(textOf(detail), /Prompt and response content were not captured when this trace was recorded/);
  assert.match(textOf(detail), /No judge results/);
});

test("captured empty content remains distinct from capture being off", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "empty-trace", provider: "anthropic", request_model: "claude-test",
    cluster_id: null, input_tokens: 12, output_tokens: 0, latency_ms: 150,
    cost_usd: null, error: null, hour: 0, prompt_redacted: "",
    response_redacted: "",
  };
  const data = bundle("evaluator-a", [sample]);
  data.providers = [{ key: "anthropic", rawProvider: "anthropic", model: "claude-test" }];

  const traceList = textOf(render(ui.Traces, createHooks(), { data }));
  assert.match(traceList, /Captured prompt was empty/);
  assert.doesNotMatch(traceList, /Content capture off/);

  const detail = render(ui.TraceDetail, createHooks(), { s: sample, onClose() {} });
  const rendered = textOf(detail);
  assert.match(rendered, /Captured prompt was empty/);
  assert.match(rendered, /Captured response was empty/);
  assert.doesNotMatch(rendered, /Content was not captured/);
});

test("trace explorer filters the newest bounded view by capture state", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const data = bundle("evaluator-a", [
    {
      trace_id: "captured-trace", provider: "openai", request_model: "gpt-test",
      prompt_redacted: "CAPTURED_PROMPT", response_redacted: "CAPTURED_RESPONSE",
      started_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    },
    {
      trace_id: "captured-empty", provider: "openai", request_model: "gpt-test",
      prompt_redacted: "", response_redacted: "", started_at: new Date().toISOString(),
    },
    {
      trace_id: "metadata-trace", provider: "openai", request_model: "gpt-test",
      prompt_redacted: null, response_redacted: null, started_at: "2026-08-20T12:00:00Z",
    },
  ]);
  data.truncation.resources.traceSamples = { available: 37, shown: 3, limit: 30 };

  let tree = render(ui.Traces, hooks, { data });
  const contentButton = findAll(
    tree,
    (node) => node.type === "button" && textOf(node) === "Content captured",
  )[0];
  contentButton.props.onClick();
  tree = render(ui.Traces, hooks, { data });
  const rendered = textOf(tree);

  assert.match(rendered, /CAPTURED_PROMPT/);
  assert.match(rendered, /Captured prompt was empty/);
  assert.doesNotMatch(rendered, /Historical metadata-only trace/);
  assert.match(rendered, /Showing newest 3 of 37 total traces/);
  assert.match(rendered, /Filters apply to this bounded view/);
  assert.match(rendered, /UTC/);
  assert.match(rendered, /minutes ago/);
  assert.doesNotMatch(rendered, /\bHour\b/);
});

test("trace detail distinguishes content, provider failure, and judge availability", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "failed-trace", provider: "custom-provider", request_model: "custom-model",
    prompt_redacted: "captured prompt", response_redacted: null,
    error: "provider failed", started_at: "2026-08-23T22:20:00Z",
  };

  const rendered = textOf(render(ui.TraceDetail, createHooks(), { s: sample, onClose() {} }));

  assert.match(rendered, /Content partially captured/);
  assert.match(rendered, /Failed trace/);
  assert.match(rendered, /No judge results/);
  assert.match(rendered, /Aug 23, 22:20 UTC/);
  assert.match(rendered, /Response was not captured for this trace/);
  assert.doesNotMatch(rendered, /response.*historical metadata-only trace/i);
});

test("drift empty state treats global trace totals as availability and opens Operations", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.driftAnalysis.current = 30;
  data.driftAnalysis.baseline = 30;
  data.driftAnalysis.readinessStatus = "global_minimum_met";
  let opened = 0;

  const tree = render(ui.Drift, createHooks(), {
    data,
    onOpenOperations: () => { opened += 1; },
  });
  const rendered = textOf(tree).replace(/\s+/g, " ");

  assert.match(rendered, /No drift analysis has completed yet/);
  assert.match(rendered, /Global trace minimum met/);
  assert.match(rendered, /Current global content-bearing traces 30 \/ 30/);
  assert.match(rendered, /Baseline global content-bearing traces 30 \/ 30/);
  assert.match(rendered, /Default current window Latest 24 hours/);
  assert.match(rendered, /Default baseline lag 24 hours/);
  assert.match(rendered, /Default baseline window Previous 7 days/);
  assert.match(rendered, /each cluster and rubric dimension has enough judged traces/);
  assert.match(rendered, /Actual job flags may use different windows or sample floors/);
  assert.match(rendered, /New traces cannot simultaneously be recent current data and historical baseline data/);
  assert.doesNotMatch(rendered, /Ready to run/);
  const operationsButton = findAll(
    tree,
    (node) => node.type === "button" && textOf(node) === "Open Operations",
  )[0];
  operationsButton.props.onClick();
  assert.equal(opened, 1);
});

test("completed zero-signal drift is distinct from an analysis that never ran", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.driftAnalysis.runStatus = "completed_no_signals";

  const rendered = textOf(render(ui.Drift, createHooks(), { data }));

  assert.match(rendered, /Completed with no signals/);
  assert.doesNotMatch(rendered, /No drift analysis has completed yet/);
});

test("overview reports insufficient readiness without calling it zero drift", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.driftAnalysis.current = 16;
  data.driftAnalysis.baseline = 0;

  const rendered = textOf(render(ui.Overview, createHooks(), { data }));

  assert.match(rendered, /Collecting current traces/);
  assert.match(rendered, /No completed run/);
  assert.doesNotMatch(rendered, /No dimensions currently clear/);
});

test("judge scores explains the empty state using shared readiness", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.driftAnalysis.current = 16;
  data.driftAnalysis.baseline = 0;

  const rendered = textOf(render(ui.Judge, createHooks(), { data })).replace(/\s+/g, " ");

  assert.match(rendered, /No eligible evaluation pipeline run has completed yet/);
  assert.match(rendered, /Current global content-bearing traces 16 \/ 30/);
  assert.match(rendered, /Baseline global content-bearing traces 0 \/ 30/);
});

test("unresolved evaluator selection is not described as a missing run", async () => {
  const ui = await loadUiModule();
  const data = bundle(null);
  data.evaluation.status = "selection_required";
  data.driftAnalysis.runStatus = "selection_required";

  const overview = textOf(render(ui.Overview, createHooks(), { data }));
  const drift = textOf(render(ui.Drift, createHooks(), { data }));
  const judge = textOf(render(ui.Judge, createHooks(), { data }));

  assert.match(overview, /Select an evaluator/);
  assert.match(drift, /Select an evaluator to view drift analysis/);
  assert.match(judge, /Select an evaluator to view judge results/);
  assert.doesNotMatch(`${overview} ${drift} ${judge}`, /No drift analysis has completed yet|No eligible evaluation pipeline run has completed yet/);
});
