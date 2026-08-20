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
  const source = `${await readFile(UI_SOURCE, "utf8")}\nexport { Dashboard, Traces, Drift, Judge, mountedApiUrl };`;
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
    meta: { totalTraces: samples.length, totalJudged: 0 },
    evaluation: { selectedId: evaluator, availableIdentities: [] },
    providers: [], clusters: [], driftSignals, dimensionOverall: [], tsRows: [],
    passrate: [], clusterPassrate: [], haikuDim: [], samples,
    providerDimension: [], evaluatorHealth: [], scoreCoverage: {},
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
