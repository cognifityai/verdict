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
  const source = `${await readFile(UI_SOURCE, "utf8")}\nexport { Dashboard, Overview, DriftSignals, Traces, TraceDetail, TabHelp, Judge, Compare, ManagementReport, mountedApiUrl };\nexport { useOperations } from "./Operations.jsx";\nexport { RegistryView } from "./Registry.jsx";\nexport { EvaluatorLab } from "./EvaluatorLab.jsx";\nexport { Runs } from "./Runs.jsx";`;
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
              "ReferenceLine", "Cell",
            ]), loader: "js" };
          }
          return { contents: componentStub([
            "Activity", "AlertTriangle", "ArrowRight", "ArrowLeft", "BarChart3",
            "Boxes", "CheckCircle2", "Clock", "Code2", "Database", "GitBranch",
            "Layers", "Scale", "Search", "Shield", "Signal", "TrendingDown",
            "Github", "Terminal", "Gauge", "FlaskConical",
            "Cpu", "DollarSign", "Filter", "X", "Sparkles", "ChevronRight",
            "Eye", "Network", "RefreshCw", "LoaderCircle",
            "Info",
          ]), loader: "js" };
        });
      },
    }],
  });
  const encoded = Buffer.from(result.outputFiles[0].text).toString("base64");
  return import(`data:text/javascript;base64,${encoded}#${Math.random()}`);
}

test("Evaluator Lab starts with a neutral response-quality rubric name", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.EvaluatorLab, createHooks(), { configUrl: "/api/config" });
  const label = findAll(
    tree,
    (node) => node.type === "label" && textOf(node).includes("Rubric name"),
  )[0];
  const input = findAll(label, (node) => node.type === "input")[0];
  assert.equal(input.props.value, "response_quality");
  const openAIOption = findAll(tree,
    (node) => node.type === "option" && textOf(node).includes("compatible endpoint"))[0];
  assert.equal(openAIOption.props.value, "openai");
});

test("Evaluator Lab makes a long judge run visible and prevents duplicate submission", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config" };

  render(ui.EvaluatorLab, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], {
    evalPackageAvailable: true,
    providers: [{ provider: "anthropic", configured: true }],
  });

  let tree = render(ui.EvaluatorLab, hooks, props);
  const previewButton = findAll(
    tree,
    (node) => node.type === "button" && textOf(node).includes("Preview eligibility"),
  )[0];
  const previewPending = previewButton.props.onClick();
  await resolveJson(requests[2], {
    eligible: 1928, alreadyJudged: 11, notEvaluable: 1859,
    plannedCalls: 1917, estimatedMaximumCostUsd: 7.6901,
    maximumOutputTokens: 981504,
    notEvaluableReasons: { response_not_captured: 1859 },
    rubric: {
      name: "response_quality", version: "1",
      dimensions: ["relevance", "completeness"],
      skippedDimensions: ["groundedness"],
    },
    planFingerprint: "plan-a", plannedTraces: ["trace-a"],
  });
  await previewPending;

  tree = render(ui.EvaluatorLab, hooks, props);
  assert.equal(findAll(
    tree,
    (node) => node.props?.label === "Already evaluated by this evaluator",
  ).length, 1);
  assert.match(textOf(tree), /Results from other evaluators remain separate/);
  assert.match(textOf(tree), /Evaluated dimensions:\s+relevance · completeness/);
  assert.match(textOf(tree), /Skipped without retrieved context:\s+groundedness/);
  const consent = findAll(
    tree,
    (node) => node.type === "input" && node.props.type === "checkbox",
  )[0];
  consent.props.onChange({ target: { checked: true } });

  tree = render(ui.EvaluatorLab, hooks, props);
  const runButton = findAll(
    tree,
    (node) => node.type === "button" && textOf(node).includes("Run 1917 judge calls"),
  )[0];
  const firstRun = runButton.props.onClick();
  await runButton.props.onClick();
  assert.equal(requests.length, 4);

  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /Evaluation running/);
  assert.match(textOf(tree), /1,917\s+planned calls/);
  assert.match(textOf(tree), /Completed results are saved as they arrive/);
  assert.equal(findAll(tree, (node) => node.props?.role === "status").length, 1);
  assert.equal(findAll(tree, (node) => node.type === "fieldset" && node.props.disabled === true).length, 1);

  await resolveJson(requests[3], {
    eligible: 1928, completed: 1917, alreadyJudged: 11, errors: 0,
    notEvaluable: 1859, availableTraces: 3787, notEvaluableReasons: {},
    evaluatorFingerprint: "evaluator-a", evaluatorId: "evaluator-id-a",
  });
  await firstRun;
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /Evaluation completed/);
  assert.doesNotMatch(textOf(tree), /Evaluation running/);

  const failedRun = findAll(
    tree,
    (node) => node.type === "button" && textOf(node).includes("Run 1917 judge calls"),
  )[0].props.onClick();
  requests[4].reject(new Error("provider unavailable"));
  await failedRun;
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /provider unavailable/);
  assert.doesNotMatch(textOf(tree), /Evaluation completed|Evaluation running/);
});

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

function createEffectHooks() {
  const states = [];
  const refs = [];
  const callbacks = [];
  const effects = [];
  let stateCursor = 0;
  let refCursor = 0;
  let callbackCursor = 0;
  let effectCursor = 0;
  let pendingEffects = [];
  const changed = (left, right) => !left || !right
    || left.length !== right.length
    || left.some((value, index) => !Object.is(value, right[index]));
  return {
    begin() {
      stateCursor = 0;
      refCursor = 0;
      callbackCursor = 0;
      effectCursor = 0;
      pendingEffects = [];
    },
    createElement(type, props, ...children) {
      return { type, props: { ...(props || {}), children } };
    },
    useState(initial) {
      const index = stateCursor++;
      if (!(index in states)) {
        states[index] = typeof initial === "function" ? initial() : initial;
      }
      const setState = (next) => {
        states[index] = typeof next === "function" ? next(states[index]) : next;
      };
      return [states[index], setState];
    },
    useRef(initial) {
      const index = refCursor++;
      if (!(index in refs)) refs[index] = { current: initial };
      return refs[index];
    },
    useCallback(fn, dependencies) {
      const index = callbackCursor++;
      if (!callbacks[index] || changed(callbacks[index].dependencies, dependencies)) {
        callbacks[index] = { value: fn, dependencies: [...dependencies] };
      }
      return callbacks[index].value;
    },
    useEffect(fn, dependencies) {
      const index = effectCursor++;
      if (!effects[index] || changed(effects[index].dependencies, dependencies)) {
        pendingEffects.push({ index, fn, dependencies: [...dependencies] });
      }
    },
    flushEffects() {
      for (const pending of pendingEffects) {
        effects[pending.index]?.cleanup?.();
        const cleanup = pending.fn();
        effects[pending.index] = {
          dependencies: pending.dependencies,
          cleanup: typeof cleanup === "function" ? cleanup : null,
        };
      }
      pendingEffects = [];
    },
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
    managementReport: {
      schema: "management-report-v1",
      scope: {
        firstCapturedAt: "2026-09-08T00:00:00+00:00",
        latestCapturedAt: "2026-09-10T00:03:00+00:00",
        calls: 28, successfulCalls: 27, failedCalls: 1, successRatePct: 96.4,
        inputTokens: 2800, outputTokens: 700, totalTokens: 3500,
        tokenKnownCalls: 28, costUsd: 0.028, costKnownCalls: 27,
        averageLatencyMs: 1000, latencyKnownCalls: 26,
        latencySampledCalls: 26, p50LatencyMs: 900, p95LatencyMs: 1800,
        identifiedApplications: 2, unattributedCalls: 0,
      },
      timeline: { availableDates: 2, shownDates: 2, rows: [
        { date: "2026-09-08", calls: 24, totalTokens: 3000, tokenKnownCalls: 24 },
        { date: "2026-09-10", calls: 4, totalTokens: 500, tokenKnownCalls: 4 },
      ] },
      applications: { availableRows: 2, shownRows: 2, rows: [
        { name: "orders-api", environments: ["production"], providers: ["openai"], models: ["gpt-5-mini"], calls: 24, successfulCalls: 24, failedCalls: 0, successRatePct: 100, inputTokens: 2400, outputTokens: 600, totalTokens: 3000, tokenKnownCalls: 24, costUsd: 0.024, costKnownCalls: 24, averageLatencyMs: 900, latencyKnownCalls: 24 },
        { name: "billing-worker", environments: ["staging"], providers: ["custom-provider"], models: ["custom-model-v1"], calls: 4, successfulCalls: 3, failedCalls: 1, successRatePct: 75, inputTokens: 400, outputTokens: 100, totalTokens: 500, tokenKnownCalls: 4, costUsd: 0.004, costKnownCalls: 3, averageLatencyMs: 1600, latencyKnownCalls: 2 },
      ] },
      models: { availableRows: 2, shownRows: 2, rows: [
        { provider: "openai", model: "gpt-5-mini", calls: 24, successfulCalls: 24, failedCalls: 0, successRatePct: 100, inputTokens: 2400, outputTokens: 600, totalTokens: 3000, tokenKnownCalls: 24, costUsd: 0.024, costKnownCalls: 24, averageLatencyMs: 900, latencyKnownCalls: 24 },
        { provider: "custom-provider", model: "custom-model-v1", calls: 4, successfulCalls: 3, failedCalls: 1, successRatePct: 75, inputTokens: 400, outputTokens: 100, totalTokens: 500, tokenKnownCalls: 4, costUsd: 0.004, costKnownCalls: 3, averageLatencyMs: 1600, latencyKnownCalls: 2 },
      ] },
    },
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

test("operations appears inside Settings only when the host configures an adapter", async () => {
  const ui = await loadUiModule();
  globalThis.window = {
    location: { hash: "#tab=settings&section=integrations", pathname: "/dashboard" },
    history: { pushState() {} }, addEventListener() {}, removeEventListener() {},
  };
  try {
    const withoutAdapter = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), operationsUrl: null });
    assert.match(textOf(withoutAdapter), /No operations adapter configured/);
    const withAdapter = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), operationsUrl: "/api/admin/operations" });
    assert.equal(findAll(withAdapter, (node) => typeof node.type === "function" && node.type.name === "Operations").length, 1);
  } finally { delete globalThis.window; }
});

test("monitoring lifecycle is one top-level workspace", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a") });
  const navigation = findAll(tree, (node) => node.type === "nav")[0];
  const labels = textOf(navigation);

  assert.match(labels, /Monitor/);
  assert.doesNotMatch(labels, /Drift/);
  assert.doesNotMatch(labels, /Drift signals/);
  assert.doesNotMatch(labels, /Registry/);
});

test("management reporting is one top-level workspace backed by the current bundle", async () => {
  const ui = await loadUiModule();
  globalThis.window = {
    location: { hash: "#tab=report&section=management", pathname: "/dashboard" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" });
    const navigation = findAll(tree, (node) => node.type === "nav")[0];
    assert.match(textOf(navigation), /Report/);
    assert.equal(findAll(
      tree,
      (node) => typeof node.type === "function" && node.type.name === "ManagementReport",
    ).length, 1);
  } finally { delete globalThis.window; }
});

test("management report actions emit aggregate downloads and invoke browser printing", async () => {
  const ui = await loadUiModule();
  const downloads = [];
  let printed = false;
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  URL.createObjectURL = (blob) => { downloads.push(blob); return `blob:report-${downloads.length}`; };
  URL.revokeObjectURL = () => {};
  globalThis.document = {
    body: { appendChild() {} },
    createElement: () => ({ click() {}, remove() {} }),
  };
  globalThis.window = { print: () => { printed = true; }, setTimeout: (callback) => callback() };
  try {
    const tree = render(ui.ManagementReport, createHooks(), { data: bundle("judge-a"), source: "live" });
    const buttons = findAll(tree, (node) => node.type === "button");
    buttons.find((button) => textOf(button).includes("Export HTML")).props.onClick();
    buttons.find((button) => textOf(button).includes("Download CSV")).props.onClick();
    buttons.find((button) => textOf(button).includes("Print / Save PDF")).props.onClick();
    assert.equal(downloads.length, 2);
    assert.equal(downloads[0].type, "text/html;charset=utf-8");
    assert.equal(downloads[1].type, "text/csv;charset=utf-8");
    assert.equal(printed, true);
  } finally {
    globalThis.document = originalDocument;
    globalThis.window = originalWindow;
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
  }
});

test("management report exposes labeled daily volume and application-level tables", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.ManagementReport, createHooks(), {
    data: bundle("judge-a"), source: "live",
  });
  const chartNode = findAll(tree,
    (node) => typeof node.type === "function" && node.type.name === "VolumeChart")[0];
  const tableNodes = findAll(tree,
    (node) => typeof node.type === "function" && node.type.name === "UtilizationTable");
  const text = [textOf(tree), textOf(render(chartNode.type, createHooks(), chartNode.props)),
    ...tableNodes.map((node) => textOf(render(node.type, createHooks(), node.props)))].join(" ");

  assert.match(text, /LLM request volume/);
  assert.match(text, /Sep 08/);
  assert.match(text, /Sep 10/);
  assert.match(text, /24/);
  assert.match(text, /Application-level LLM utilization/);
  assert.match(text, /orders-api/);
  assert.match(text, /Model performance and throughput/);
  assert.match(text, /gpt-5-mini/);
  assert.match(text, /Report scope/);
  assert.doesNotMatch(text, /Capacity & Cost Simulator/);
  assert.doesNotMatch(text, /Recent LLM activity/);
});

test("Monitor keeps fixed-window signals as clearly labeled legacy history", async () => {
  const ui = await loadUiModule();
  let pushed = null;
  globalThis.window = {
    location: { hash: "#tab=monitor&section=signals&evaluator=judge-a", pathname: "/dashboard" },
    history: { pushState: (_state, _title, url) => { pushed = url; }, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const data = bundle("judge-a", [], [{
      id: "signal-1", clusterId: "incident", clusterLabel: "Incident response",
      dimension: "instruction_following", direction: "regression",
      provider: "openai", providerLabel: "OpenAI · test-model",
      statName: "fisher_exact", stat: 5.2, p: 0.001, pAdj: 0.004,
      cliffsDelta: -0.55, cohensD: -1.2, nCur: 80, nBase: 80,
      layers: ["judge_rubric"], exampleTraceIds: ["trace-1"],
      action: "Review the response-format regression.",
    }]);
    data.clusters = [{ cluster_id: "incident", display_name: "Incident response", n: 160 }];
    data.driftRun = { id: "run-1", signalCount: 1, completedAt: "2026-09-07T22:00:00Z" };
    data.driftAnalysis.runStatus = "completed_with_signals";
    data.evaluation.availableIdentities = [{ id: "judge-a", label: "Response quality", complete: true }];

    const tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    assert.equal(findAll(tree, (node) => node.type === "select" && node.props["aria-label"] === "Evaluator identity").length, 1);
    const page = findAll(tree,
      (node) => typeof node.type === "function" && node.type.name === "DriftSignals")[0];
    assert.ok(page);

    const rendered = render(ui.DriftSignals, createHooks(), page.props);
    assert.match(textOf(rendered), /Legacy fixed-window history/i);
    assert.match(textOf(rendered), /read-only/i);
    assert.match(textOf(rendered), /Instruction.following/);
    assert.match(textOf(rendered), /Incident response/);
    assert.match(textOf(rendered), /OpenAI · test-model/);
    assert.match(textOf(rendered), /Review the response-format regression/);
    const stats = findAll(rendered,
      (node) => typeof node.type === "function" && node.type.name === "SignalStat");
    assert.equal(stats.find((node) => node.props.label === "Samples").props.value, "80 vs 80");

    const traceButton = findAll(rendered,
      (node) => node.type === "button" && node.props.title === "trace-1")[0];
    traceButton.props.onClick();
    assert.equal(pushed, "#tab=explore&section=calls&trace=trace-1&evaluator=judge-a");
  } finally { delete globalThis.window; }
});

test("overview and navigation use only the active monitor as current drift", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a", [], [{ id: "signal-1", direction: "regression" }]);
  data.driftRun = { id: "run-1", signalCount: 1 };
  data.driftAnalysis.runStatus = "completed_with_signals";
  data.monitor = {
    active: {
      snapshot: { comparison: { metrics: [{ alert: true }] } },
    },
    candidate: null,
  };

  const tree = render(ui.Overview, createHooks(), { data, includeMonitor: false });
  const metrics = findAll(tree,
    (node) => typeof node.type === "function" && node.type.name === "MetricCell");
  const byLabel = Object.fromEntries(metrics.map((node) => [node.props.label, node.props]));
  assert.equal(byLabel["Cohort monitor alerts"].value, 1);
  assert.equal(byLabel["Evaluation drift signals"], undefined);

  data.monitor.candidate = {
    snapshot: { comparison: { metrics: [{ alert: true }] } },
  };
  const dashboard = render(ui.Dashboard, createHooks(), { data });
  const monitorButton = findAll(dashboard,
    (node) => node.type === "button" && textOf(node).includes("Monitor"))[0];
  assert.equal(textOf(monitorButton).trim(), "Monitor 1");
});

test("bundled sample demonstrates the current Monitor instead of only legacy signals", async () => {
  const ui = await loadUiModule();
  const dashboard = render(ui.Dashboard, createHooks(), {});
  const monitorButton = findAll(dashboard,
    (node) => node.type === "button" && textOf(node).includes("Monitor"))[0];
  assert.equal(textOf(monitorButton).trim(), "Monitor 1");
  assert.match(textOf(dashboard), /Latest completed prospective comparison/i);
});

test("legacy signal history distinguishes no run from a completed zero-signal run", async () => {
  const ui = await loadUiModule();
  const noRun = textOf(render(ui.DriftSignals, createHooks(), { data: bundle("judge-a") }));
  assert.match(noRun, /No fixed-window drift analysis has completed/i);
  assert.match(noRun, /New comparisons are created in Monitor → Compare History/i);
  assert.doesNotMatch(noRun, /pipeline needs separate/i);

  const completed = bundle("judge-a");
  completed.driftRun = { id: "run-zero", signalCount: 0, completedAt: "2026-09-07T22:00:00Z" };
  completed.driftAnalysis.runStatus = "completed_no_signals";
  const zero = textOf(render(ui.DriftSignals, createHooks(), { data: completed }));
  assert.match(zero, /Completed with no signals/i);
  assert.doesNotMatch(zero, /No fixed-window drift analysis has completed/i);
});

test("an inconsistent legacy run does not advertise its stale signal count", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.driftRun = { id: "inconsistent-run", signalCount: 2 };
  data.driftAnalysis.runStatus = "no_completed_run";
  data.evaluation.driftStatus = "inconsistent_run";

  const overview = render(ui.Overview, createHooks(), {
    data, includeMonitor: false, onOpenSignals() {},
  });
  const signalMetric = findAll(overview,
    (node) => typeof node.type === "function" && node.type.name === "MetricCell")
    .find((node) => node.props.label === "Evaluation drift signals");
  assert.equal(signalMetric, undefined);

  const dashboard = render(ui.Dashboard, createHooks(), { data });
  const monitorButton = findAll(dashboard,
    (node) => node.type === "button" && textOf(node).includes("Monitor"))[0];
  assert.equal(textOf(monitorButton).trim(), "Monitor");
});

test("legacy fixed-window totals remain truthful when cards are bounded", async () => {
  const ui = await loadUiModule();
  const shownSignals = Array.from({ length: 40 }, (_, index) => ({
    id: `signal-${index}`, dimension: "relevance", direction: "regression",
  }));
  const data = bundle("judge-a", [], shownSignals);
  data.driftRun = { id: "run-bounded", signalCount: 60 };
  data.driftAnalysis.runStatus = "completed_with_signals";
  data.truncation = {
    applied: true,
    resources: { driftSignals: { available: 60, shown: 40, limit: 40 } },
  };

  const page = textOf(render(ui.DriftSignals, createHooks(), { data }));
  assert.match(page, /60 signals/i);
  assert.match(page, /showing 40/i);

  const overview = render(ui.Overview, createHooks(), { data, includeMonitor: false });
  const signalMetric = findAll(overview,
    (node) => typeof node.type === "function" && node.type.name === "MetricCell")
    .find((node) => node.props.label === "Evaluation drift signals");
  assert.equal(signalMetric, undefined);
});

test("fixed-window signal cards reject malformed unbounded evidence lists", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a", [], [{
    id: "malformed", dimension: "custom_dimension", direction: "regression",
    layers: [null, {}, "valid-layer", ...Array.from({ length: 30 }, (_, index) => `layer-${index}`)],
    exampleTraceIds: [null, {}, "trace-1", ...Array.from({ length: 30 }, (_, index) => `trace-${index + 2}`)],
    action: { unexpected: true },
  }]);
  data.driftRun = { id: "run-malformed", signalCount: 1 };
  data.driftAnalysis.runStatus = "completed_with_signals";

  const page = render(ui.DriftSignals, createHooks(), { data });
  assert.match(textOf(page), /Review the affected traces/);
  assert.equal(findAll(page,
    (node) => node.type === "button" && node.props.title?.startsWith("trace-")).length, 5);
});

test("dashboard exposes only the six product workspaces", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a") });
  const navigation = findAll(tree, (node) => node.type === "nav")[0];
  const labels = textOf(navigation);

  for (const label of ["Overview", "Explore", "Evaluate", "Monitor", "Report", "Settings"]) {
    assert.match(labels, new RegExp(label));
  }
  for (const oldLabel of ["Findings", "Reliability", "Performance", "Behavior", "Agent runs", "Trace explorer", "Judge scores", "Evaluators", "Compare LLMs"]) {
    assert.doesNotMatch(labels, new RegExp(oldLabel));
  }
});

test("overview prioritizes a stored historical monitor alert over absent legacy drift", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.monitor = {
    state: "candidate",
    active: null,
    candidate: {
      state: "candidate",
      policy: { evaluator_fingerprint: "judge-a", grouping_mode: "none" },
      snapshot: {
        manifest: { reference_unit_ids: Array(80).fill("r"), current_unit_ids: Array(20).fill("c") },
        comparison: {
          status: "alert", alpha_threshold: 0.05,
          metrics: [{ metric: "judge.safety.pass", alert: true,
            reference_value: 0.9, current_value: 0.6, effect: -0.3,
            p_adjusted: 0.01, reference_n: 80, current_n: 20 }],
          metric_coverage: [], groups: [],
        },
      },
    },
  };

  const text = textOf(render(ui.Overview, createHooks(), { data }));
  assert.match(text, /exploratory historical comparison/i);
  assert.match(text, /safety pass rate/i);
  assert.doesNotMatch(text, /No completed run/i);
});

test("overview shows the authoritative active monitor beside a newer preview", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  const response = (state, status, metric) => ({
    state,
    policy: { evaluator_fingerprint: "judge-a", grouping_mode: "none" },
    snapshot: {
      manifest: {
        reference_unit_ids: Array(8).fill("r"),
        current_unit_ids: Array(2).fill("c"),
        prospective_open: false,
      },
      comparison: {
        status, alpha_threshold: 0.05,
        metrics: [{ metric: `judge.${metric}.pass`, alert: status === "alert",
          reference_value: 0.9, current_value: 0.6, effect: -0.3,
          p_adjusted: 0.01, reference_n: 8, current_n: 2 }],
        metric_coverage: [], groups: [],
      },
    },
  });
  data.monitor = {
    state: "active",
    active: response("active", "alert", "active_quality"),
    candidate: response("candidate", "no_alert", "candidate_quality"),
  };

  const text = textOf(render(ui.Overview, createHooks(), { data }));
  assert.match(text, /ACTIVE MONITOR/);
  assert.match(text, /active quality pass rate/i);
  assert.match(text, /EXPLORATORY HISTORICAL COMPARISON/);
  assert.match(text, /candidate quality pass rate/i);
});

test("overview distinguishes evaluator waiting from traffic collection", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.monitor = {
    state: "active", candidate: null,
    active: {
      state: "active",
      policy: { evaluator_fingerprint: "judge-a", grouping_mode: "none",
        prospective_target: 2 },
      snapshot: {
        manifest: { reference_unit_ids: ["r1", "r2"],
          current_unit_ids: ["c1", "c2"], prospective_open: true,
          pending_evaluator_units: [{ unit_id: "c1" }, { unit_id: "c2" }] },
        comparison: { status: "insufficient", alpha_threshold: 0.05,
          metrics: [], metric_coverage: [], groups: [] },
      },
    },
  };

  const text = textOf(render(ui.Overview, createHooks(), { data }));
  assert.match(text, /Awaiting 2 evaluator results/);
  assert.match(text, /Run the selected evaluator/);
  assert.doesNotMatch(text, /Collecting 2\/2/);
});

test("Trace Explorer renders execution and evaluation as separate states", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "trace-1", provider: "anthropic", request_model: "model-a",
    providerStatus: "provider_succeeded", judgeStatus: "not_judged",
    prompt_redacted: "Question", response_redacted: "Answer",
    input_tokens: 2, output_tokens: 3, started_at: "2026-09-01T00:00:00Z",
  };

  const tree = render(ui.Traces, createHooks(), {
    data: bundle(null, [sample]), source: "live",
  });
  const text = textOf(tree);

  assert.match(text, /Execution/);
  assert.match(text, /Evaluation/);
  assert.match(text, /provider succeeded/i);
  assert.match(text, /not evaluated/i);
});

test("Compare explains why captured Codex runs may not be LLM traces", async () => {
  const ui = await loadUiModule();
  const data = bundle(null);
  data.meta.agentRunSources = [{ sourceKind: "codex", runs: 9 }];
  data.providers = [{
    key: "anthropic", rawProvider: "anthropic", label: "Anthropic",
    model: "claude", n: 2, errors: 0, errorRate: 0, avgLatency: 0,
    inTok: 2, outTok: 3, cost: 0, passRate: null, judged: 0,
  }];

  const tree = render(ui.Compare, createHooks(), { data, source: "live" });

  assert.match(textOf(tree), /9\s+Codex agent runs were captured/i);
  assert.match(textOf(tree), /not included in LLM trace comparisons/i);
});

test("Reliability heading exposes a visible, accessible evidence explanation", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  let tree = render(ui.TabHelp, hooks, {
    tab: "reliability", label: "Reliability",
    text: "Reliability shows execution outcomes.",
  });
  const wrapper = findAll(tree, (node) => node.type === "span")[0];
  const button = findAll(tree, (node) => node.type === "button")[0];
  assert.equal(button.props["aria-label"], "About Reliability");
  assert.equal(findAll(tree, (node) => node.props?.role === "tooltip").length, 0);

  wrapper.props.onMouseEnter();
  tree = render(ui.TabHelp, hooks, {
    tab: "reliability", label: "Reliability",
    text: "Reliability shows execution outcomes.",
  });
  const [tooltip] = findAll(tree, (node) => node.props?.role === "tooltip");
  assert.match(textOf(tooltip), /execution outcomes/);
  assert.equal(findAll(tree, (node) => node.type === "button")[0].props["aria-expanded"], true);

  findAll(tree, (node) => node.type === "span")[0].props.onMouseLeave();
  tree = render(ui.TabHelp, hooks, {
    tab: "reliability", label: "Reliability",
    text: "Reliability shows execution outcomes.",
  });
  assert.equal(findAll(tree, (node) => node.props?.role === "tooltip").length, 0);
});

test("Trace filters preserve the routed evaluator identity", async () => {
  const ui = await loadUiModule();
  let pushed = null;
  globalThis.window = {
    location: {
      hash: "#tab=traces&evaluator=evaluator-a", pathname: "/dashboard",
    },
    history: { pushState: (_state, _title, url) => { pushed = url; } },
  };
  try {
    const tree = render(ui.Dashboard, createHooks(), {
      data: bundle("evaluator-a"), source: "live",
    });
    const traces = findAll(
      tree,
      (node) => typeof node.type === "function" && node.type.name === "Traces",
    )[0];
    traces.props.onJudgeStatus("judged");

    assert.match(
      pushed, /^#tab=explore&section=calls&judge=judged&evaluator=evaluator-a$/,
    );
  } finally {
    delete globalThis.window;
  }
});

test("Evaluator result navigation selects the evaluator that just ran", async () => {
  const ui = await loadUiModule();
  let pushed = null;
  let selected = null;
  globalThis.window = {
    location: {
      hash: "#tab=evaluators&evaluator=old-evaluator", pathname: "/dashboard",
    },
    history: { pushState: (_state, _title, url) => { pushed = url; } },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const tree = render(ui.Dashboard, createHooks(), {
      data: bundle("old-evaluator"),
      source: "live",
      onEvaluatorChange: (evaluatorId) => { selected = evaluatorId; },
    });
    const lab = findAll(
      tree,
      (node) => typeof node.type === "function" && node.type.name === "EvaluatorLab",
    )[0];

    lab.props.onOpenEvaluated("new-evaluator");

    assert.equal(selected, "new-evaluator");
    assert.equal(pushed, "#tab=explore&section=calls&judge=judged&evaluator=new-evaluator");
  } finally {
    delete globalThis.window;
  }
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
  assert.match(rendered, /Mechanical activation checks/);
  assert.match(rendered, /Mechanically passed/);
  assert.match(rendered, /do not establish semantic cluster quality/);
  assert.doesNotMatch(rendered, /Validated/);
  assert.match(rendered, /Redacted billing prompt/);
  assert.match(rendered, /Baseline\s+20\s+\/\s+30/);
  assert.match(rendered, /split across many small clusters/);
  assert.match(rendered, /ward-best-k-v2/);
  assert.match(rendered, /No explicit intent key was captured/);
  assert.match(rendered, /Use these clusters/);
  assert.match(rendered, /Refit active/);
  assert.match(rendered, /Rollback to version/);

  for (const label of ["Refit active", "Use these clusters", "Rollback to version"]) {
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

function runListRow(runId) {
  return {
    runId, sourceKind: "codex", startedAt: "2026-09-01T00:00:00Z",
    status: "completed", sourceOutcome: "completed", agentName: runId,
    turnCount: 1, eventCount: 1, findings: [], metrics: {},
    evidenceCoverage: {}, turnOutcomes: {}, findingSeverity: {},
    evaluationCoverage: { state: "not_selected" },
  };
}

function runListPage(offset, runIds, available = 61) {
  return {
    summary: { available, shown: runIds.length },
    page: {
      available, shown: runIds.length, offset, limit: 30,
      truncated: offset + runIds.length < available,
    },
    runs: runIds.map(runListRow),
  };
}

test("Agent Runs pages the full list and keeps an empty last page recoverable", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const selections = [];
  const props = {
    url: "/api/runs",
    onSelectRun(runId) { selections.push(runId); },
  };

  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.match(requests[0].url, /limit=30/);
  assert.match(requests[0].url, /offset=0/);
  await resolveJson(requests[0], runListPage(0, ["run-61", "run-60"]));

  let tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.match(textOf(tree), /Showing 1–2 of 61 runs/);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Next")[0]
    .props.onClick();
  tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const secondRequest = requests.find((request) => /offset=30/.test(request.url));
  assert.ok(secondRequest);
  assert.deepEqual(selections, [null]);
  await resolveJson(secondRequest, runListPage(30, ["run-31"]));

  tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.match(textOf(tree), /Showing 31–31 of 61 runs/);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Next")[0]
    .props.onClick();
  tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const emptyRequest = requests.find((request) => /offset=60/.test(request.url));
  assert.ok(emptyRequest);
  await resolveJson(emptyRequest, runListPage(60, []));

  tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.match(textOf(tree), /No runs on this page/);
  const previous = findAll(
    tree, (node) => node.type === "button" && textOf(node) === "Previous",
  )[0];
  assert.equal(previous.props.disabled, false);
});

test("Agent Runs presents component-only token evidence as partial", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = {
    ...runListRow("run-partial"),
    sourceTokenUsage: {
      totalTokens: null, turns: 1, state: "partial",
      inputTokens: 10, cachedInputTokens: null, cacheWriteInputTokens: null,
      outputTokens: null, reasoningOutputTokens: null,
    },
  };

  render(ui.Runs, hooks, { url: "/api/runs" });
  hooks.flushEffects();
  await resolveJson(requests[0], {
    summary: { available: 1, shown: 1 },
    page: { available: 1, shown: 1, offset: 0, limit: 30, truncated: false },
    runs: [run],
  });
  let tree = render(ui.Runs, hooks, { url: "/api/runs" });
  hooks.flushEffects();
  const detailRequest = requests.find((request) => request.url.includes("run-partial"));
  assert.ok(detailRequest);
  await resolveJson(detailRequest, {
    turns: [{
      turnId: "turn-partial", sequence: 0, status: "completed",
      request: "request", response: "response",
      requestState: "present", responseState: "present",
      requestTruncated: false, responseTruncated: false,
      tokenUsage: {
        inputTokens: 10, cachedInputTokens: null, cacheWriteInputTokens: null,
        outputTokens: null, reasoningOutputTokens: null, totalTokens: null,
        basis: "claude_provider_response_sum",
      },
    }],
    turnPage: { available: 1, shown: 1, offset: 0, limit: 20, truncated: false },
    events: [], producerCount: 0,
    page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false },
  });

  tree = render(ui.Runs, hooks, { url: "/api/runs" });
  const detail = findAll(
    tree,
    (node) => typeof node.type === "function" && node.type.name === "RunDetail",
  )[0];
  assert.ok(detail);
  const rendered = textOf(render(detail.type, createHooks(), detail.props));
  assert.match(rendered, /10 input tokens/);
  assert.match(rendered, /partial/);
  assert.match(rendered, /total unavailable/);
  assert.doesNotMatch(rendered, /source tokens not captured/);
  assert.doesNotMatch(rendered, /Source-reported token usage: not captured/);
});

test("Agent Runs ignores an older list response after its query changes", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();

  render(ui.Runs, hooks, { url: "/api/runs", evaluatorFingerprint: "old" });
  hooks.flushEffects();
  render(ui.Runs, hooks, { url: "/api/runs", evaluatorFingerprint: "new" });
  hooks.flushEffects();
  assert.equal(requests.length, 2);

  await resolveJson(requests[1], runListPage(0, ["new-run"], 1));
  await resolveJson(requests[0], runListPage(0, ["old-run"], 1));
  const tree = render(
    ui.Runs, hooks, { url: "/api/runs", evaluatorFingerprint: "new" },
  );
  hooks.flushEffects();

  assert.match(textOf(tree), /new-run/);
  assert.doesNotMatch(textOf(tree), /old-run/);
});

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

test("live provider comparison does not attribute unmatched drift to a provider card", async () => {
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

  assert.equal((rendered.match(/regressed/g) || []).length, 0);
  assert.match(rendered, /Unmatched traffic/);
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
  assert.match(textOf(detail), /No evaluation result/);
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
  assert.match(rendered, /Showing newest 3 of 37 application traces/);
  assert.match(rendered, /Judge telemetry remains in store totals and is excluded here/);
  assert.match(rendered, /Filters apply to this bounded view/);
  assert.match(rendered, /UTC/);
  assert.match(rendered, /minutes ago/);
  assert.doesNotMatch(rendered, /\bHour\b/);
});

test("live trace explorer pages through the existing bounded trace samples", async () => {
  const ui = await loadUiModule();
  const offsets = [];
  const data = bundle("evaluator-a", [
    { trace_id: "trace-030", prompt_redacted: "page two", response_redacted: "response" },
  ]);
  data.truncation.resources.traceSamples = { available: 65, shown: 1, limit: 30 };

  const tree = render(ui.Traces, createHooks(), {
    data,
    source: "live",
    traceOffset: 30,
    reloading: false,
    onTracePageChange(offset) { offsets.push(offset); },
  });
  const previous = findAll(
    tree,
    (node) => node.type === "button" && textOf(node) === "Previous",
  )[0];
  const next = findAll(
    tree,
    (node) => node.type === "button" && textOf(node) === "Next",
  )[0];

  assert.match(textOf(tree), /Showing 31–31 of 65 application traces/);
  previous.props.onClick();
  next.props.onClick();
  assert.deepEqual(offsets, [0, 60]);
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
  assert.match(rendered, /No evaluation result/);
  assert.match(rendered, /Aug 23, 22:20 UTC/);
  assert.match(rendered, /Response was not captured for this trace/);
  assert.doesNotMatch(rendered, /response.*historical metadata-only trace/i);
});

test("trace detail identifies a successful tool-only response without calling it uncaptured", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "tool-trace", provider: "anthropic", request_model: "claude-test",
    prompt_redacted: "inspect the repository", response_redacted: null,
    finish_reason: "tool_use", error: null, started_at: "2026-08-23T22:20:00Z",
    deterministicFacts: {
      providerOutcome: "succeeded", promptPresent: true, responsePresent: false,
      judgeEligible: false, notEvaluableReason: "response_not_captured",
      responseCharacters: null, validJson: null, refusalSignature: null,
      apologyStart: null, hedgePhrases: null,
    },
  };

  const tree = render(ui.TraceDetail, createHooks(), { s: sample, onClose() {} });
  const rendered = textOf(tree);
  const responseEvidence = findAll(
    tree,
    (node) => node.type?.name === "TraceFact" && node.props?.label === "Response evidence",
  )[0];

  assert.match(rendered, /model call returned tool calls without assistant text/i);
  assert.equal(responseEvidence.props.value, "No assistant text (tool use)");
  assert.doesNotMatch(rendered, /Response was not captured for this trace/);
});

test("trace detail exposes bounded judge-free facts without claiming quality", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "facts-trace", provider: "openai", request_model: "gpt-test",
    operation: "chat", prompt_redacted: "return json",
    response_redacted: "{\"ok\":true}", finish_reason: "stop",
    input_tokens: 10, output_tokens: 4, latency_ms: 120, cost_usd: 0.001,
    deterministicFacts: {
      providerOutcome: "succeeded", promptPresent: true, responsePresent: true,
      judgeEligible: true, notEvaluableReason: null, responseCharacters: 11,
      validJson: true, refusalSignature: false, apologyStart: false,
      hedgePhrases: 0,
    },
  };

  const tree = render(ui.TraceDetail, createHooks(), { s: sample, onClose() {} });
  const rendered = textOf(tree).replace(/\s+/g, " ");
  const factValues = Object.fromEntries(findAll(
    tree, (node) => node.type?.name === "TraceFact",
  ).map((node) => [node.props.label, node.props.value]));

  assert.match(rendered, /JUDGE-FREE TRACE FACTS/);
  assert.equal(factValues["Provider outcome"], "succeeded");
  assert.equal(factValues["Judge evidence"], "Eligible");
  assert.equal(factValues["Valid JSON"], "Yes");
  assert.equal(factValues["Refusal signature"], "Not detected");
  assert.equal(factValues.Tokens, "10 in / 4 out");
  assert.match(rendered, /do not determine correctness, sentiment, or response quality/);
});

test("overview does not confuse legacy readiness with cohort monitor status", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.driftAnalysis.current = 16;
  data.driftAnalysis.baseline = 0;

  const rendered = textOf(render(ui.Overview, createHooks(), { data }));

  assert.match(rendered, /monitoring/i);
  assert.match(rendered, /Not configured/);
  assert.doesNotMatch(rendered, /No dimensions currently clear/);
});

test("judge scores directs an empty store to Evaluator Lab without legacy windows", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");
  data.driftAnalysis.current = 16;
  data.driftAnalysis.baseline = 0;

  const rendered = textOf(render(ui.Judge, createHooks(), { data })).replace(/\s+/g, " ");

  assert.match(rendered, /No evaluator results have been stored yet/);
  assert.match(rendered, /Evaluator Lab stores results for an eligible trace set/);
  assert.doesNotMatch(rendered, /global content-bearing traces/);
});

test("unresolved legacy evaluator selection is confined to legacy history", async () => {
  const ui = await loadUiModule();
  const data = bundle(null);
  data.evaluation.status = "selection_required";
  data.driftAnalysis.runStatus = "selection_required";

  const overviewTree = render(ui.Overview, createHooks(), { data });
  const overview = textOf(overviewTree);
  const judge = textOf(render(ui.Judge, createHooks(), { data }));
  const signalMetric = findAll(overviewTree,
    (node) => typeof node.type === "function" && node.type.name === "MetricCell")
    .find((node) => node.props.label === "Evaluation drift signals");

  assert.equal(signalMetric, undefined);
  assert.doesNotMatch(overview, /Evaluation drift signals/);
  assert.match(judge, /Select an evaluator to view judge results/);
  assert.doesNotMatch(judge, /No evaluator results have been stored yet/);
});
