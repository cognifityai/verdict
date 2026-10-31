import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { build } from "esbuild";

const UI_SOURCE = new URL("../VerdictUI.jsx", import.meta.url);
let uiCompileCount = 0;

test("UI test modules are isolated without recompiling the dashboard", async () => {
  const before = uiCompileCount;
  const first = await loadUiModule();
  const second = await loadUiModule();
  assert.notEqual(first, second);
  assert.ok(uiCompileCount - before <= 1);
});

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

let compiledUi;

async function compileUiModule() {
  const source = `${await readFile(UI_SOURCE, "utf8")}\nexport { Dashboard, GuidedHome, Traces, TraceDetail, TraceSpans, TabHelp, Judge, Compare, ManagementReport, mountedApiUrl, Monitor };\nexport { useOperations } from "./Operations.jsx";\nexport { Registry, RegistryView, RunOutcomeSummary, RunOutcomePreview } from "./Registry.jsx";\nexport { EvaluatorLab } from "./EvaluatorLab.jsx";\nexport { ConversationEvaluation } from "./ConversationEvaluation.jsx";\nexport { MatchedConversationCompare } from "./MatchedConversationCompare.jsx";\nexport { Runs, TurnAnalysis } from "./Runs.jsx";\nexport { Usage, UsageChart, DayIssueSummary, DayExplanation, DayAiExplanation } from "./Usage.jsx";\nexport { EvidenceExplain } from "./EvidenceExplain.jsx";\nexport { Experiments } from "./Experiments.jsx";\nexport { ScopedEvaluate } from "./ScopedEvaluate.jsx";\nexport { Insights } from "./Insights.jsx";\nexport { SetupWizard } from "./SetupWizard.jsx";\nexport { ControlCenter } from "./ControlCenter.jsx";\nexport { IssueInbox } from "./IssueInbox.jsx";`;
  uiCompileCount += 1;
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
  return Buffer.from(result.outputFiles[0].text).toString("base64");
}

async function loadUiModule() {
  compiledUi ??= compileUiModule();
  const encoded = await compiledUi;
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

test("Evaluator Lab explains why partial model calls are excluded from judging", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config" };
  render(ui.EvaluatorLab, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  await resolveJson(requests[1], { evalPackageAvailable: true,
    providers: [{ provider: "anthropic", configured: true }] });
  const before = render(ui.EvaluatorLab, hooks, props);
  const preview = findAll(before, (node) => node.type === "button" &&
    textOf(node).includes("Preview eligible calls"))[0].props.onClick();
  await resolveJson(requests[2], {
    unit: "trace", availableTraces: 1, eligible: 0, alreadyJudged: 0,
    notEvaluable: 1, notEvaluableReasons: { trace_unfinished: 1 }, plannedCalls: 0,
    estimatedMaximumCostUsd: 0, maximumOutputTokens: 0,
    rubric: { dimensions: ["relevance"], skippedDimensions: [] },
    plannedTraces: [], planFingerprint: "unused",
  });
  await preview;
  const after = textOf(render(ui.EvaluatorLab, hooks, props));
  assert.match(after, /Unfinished model call: 1/);
  assert.doesNotMatch(after, /trace_unfinished/);
});

test("conversation upload, approved run, and partial coverage refresh", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", token: "setup-token", provider: "anthropic", model: "synthetic",
    providerState: { configured: true, customEndpointConfigured: true }, updatePreferences: () => {} };
  const document = { name: "reply_quality", version: "1", target: "response",
    dimensions: [{ name: "relevance", description: "Addresses the request.", type: "binary" }] };
  let tree = render(ui.ConversationEvaluation, hooks, props);
  const file = findAll(tree, (node) => node.type === "input" && node.props.type === "file")[0];
  const uploading = file.props.onChange({ target: { files: [{ size: 200,
    text: async () => JSON.stringify(document) }] } });
  await new Promise(setImmediate);
  await resolveJson(requests[0], { ...document, fingerprint: "a".repeat(64) });
  await uploading;

  tree = render(ui.ConversationEvaluation, hooks, props);
  const previewButton = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview first page"))[0];
  const loading = previewButton.props.onClick();
  await resolveJson(requests[1], { target: "response", scannedConversations: 1,
    eligibleTargets: 2, alreadyJudged: 0, plannedCalls: 1, retryableErrors: 0,
    notEvaluableReasons: {}, plannedTargets: [{ conversationId: "a", revision: "one",
      targetPosition: 1 }], planFingerprint: "plan", evaluatorFingerprint: "b".repeat(64) });
  await resolveJson(requests[2], { conversations: [{ id: "a".repeat(32),
    event_at: "2026-09-01T00:00:00Z", end_status: "complete", input_issues: [],
    coverage: { targets: 2, completed: 0, error: 0, missing: 2, fullyGraded: false } }] });
  await loading;

  tree = render(ui.ConversationEvaluation, hooks, props);
  assert.match(textOf(tree), /configured Anthropic endpoint/);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.ConversationEvaluation, hooks, props);
  const runButton = findAll(tree, (node) => node.type === "button" &&
    /Run\s+1\s+judge calls/.test(textOf(node)))[0];
  assert.ok(runButton, textOf(tree));
  const running = runButton.props.onClick();
  assert.equal(JSON.parse(requests[3].options.body).planFingerprint, "plan");
  assert.equal(JSON.parse(requests[3].options.body).confirmExternalEgress, true);
  await resolveJson(requests[3], { completed: 1, errors: 0, stale: 0 });
  await resolveJson(requests[4], { target: "response", scannedConversations: 1,
    eligibleTargets: 2, alreadyJudged: 1, plannedCalls: 1, retryableErrors: 0,
    notEvaluableReasons: {}, plannedTargets: [{ conversationId: "a", revision: "one",
      targetPosition: 3 }], planFingerprint: "plan-next", evaluatorFingerprint: "b".repeat(64) });
  await resolveJson(requests[5], { conversations: [{ id: "a".repeat(32),
    event_at: "2026-09-01T00:00:00Z", end_status: "complete", input_issues: [],
    coverage: { targets: 2, completed: 1, error: 0, missing: 1, fullyGraded: false } }] });
  await running;
  tree = render(ui.ConversationEvaluation, hooks, props);
  assert.match(textOf(tree), /1\/2 completed/);
  assert.match(textOf(tree), /Saved:\s+1/);
  assert.doesNotMatch(textOf(tree), /Fully graded/);
  assert.equal(requests.length, 6);
});

test("conversation approval is cleared after failed navigation and run", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", token: "setup-token", provider: "openai", model: "synthetic",
    providerState: { configured: true }, updatePreferences: () => {} };
  const document = { name: "quality", version: "1", target: "conversation",
    dimensions: [{ name: "helpful", description: "Addresses the request.", type: "binary" }] };
  let tree = render(ui.ConversationEvaluation, hooks, props);
  const upload = findAll(tree, (node) => node.type === "input" && node.props.type === "file")[0]
    .props.onChange({ target: { files: [{ size: 200, text: async () => JSON.stringify(document) }] } });
  await new Promise(setImmediate);
  await resolveJson(requests[0], { ...document, fingerprint: "a".repeat(64) });
  await upload;
  tree = render(ui.ConversationEvaluation, hooks, props);
  const load = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview first page"))[0].props.onClick();
  const preview = { target: "conversation", scannedConversations: 1,
    eligibleTargets: 1, alreadyJudged: 0, plannedCalls: 1, retryableErrors: 0,
    notEvaluableReasons: {}, plannedTargets: [{ conversationId: "a", revision: "one",
      targetPosition: null }], planFingerprint: "plan", evaluatorFingerprint: "b".repeat(64) };
  await resolveJson(requests[1], preview);
  await resolveJson(requests[2], { conversations: [], nextCursor: "next" });
  await load;
  tree = render(ui.ConversationEvaluation, hooks, props);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.ConversationEvaluation, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.checked, true);
  const navigating = findAll(tree, (node) => node.type === "button" &&
    textOf(node) === "Next page by ID")[0].props.onClick();
  await resolveJson(requests[3], preview);
  requests[4].resolve({ ok: false, status: 503, json: async () => ({ error: "review unavailable" }) });
  await navigating;
  tree = render(ui.ConversationEvaluation, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.checked, false);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.ConversationEvaluation, hooks, props);
  const running = findAll(tree, (node) => node.type === "button" &&
    /Run\s+1\s+judge calls/.test(textOf(node)))[0].props.onClick();
  requests[5].resolve({ ok: false, status: 400, json: async () => ({ error: "stale plan" }) });
  await running;
  tree = render(ui.ConversationEvaluation, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.checked, false);
});

test("privacy settings do not offer an unenforced Agent Run retention control", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "privacy" };
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], {
    documents: [{ kind: "settings", documentId: "default", state: "active", revision: 1,
      payload: { captureContent: true, retentionDays: 30, providerKeyEnvVars: [] } }],
    worker: { status: "unverified" }, dailyOperations: { localAgentSources: [] },
    notifications: [], deliveryAttempts: [], reviewQueue: [],
  });
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  const settled = render(ui.ControlCenter, hooks, props);
  assert.equal(findAll(settled, (node) => node.type === "input" && node.props.type === "number").length, 0);
  assert.doesNotMatch(textOf(settled), /Retention days/);
  assert.match(textOf(settled), /manage stored evidence retention outside Verdict/);
});

test("alert queue loads by a stable position and deduplicates a changed row", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "alerts" };
  const row = (index) => ({ intentId: index.toString(16).padStart(64, "0"),
    createdAt: `2026-10-18T00:${String(index).padStart(2, "0")}:00+00:00`,
    status: "pending", kind: "finding", destination: "local_log", attemptCount: 0 });
  const firstRows = Array.from({ length: 100 }, (_, index) => row(index));
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { documents: [], worker: { status: "unverified" },
    dailyOperations: { localAgentSources: [] }, notifications: [], deliveryAttempts: [],
    reviewQueue: [], deliveryQueue: firstRows,
    deliveryQueueCounts: { pending: 102 } });
  let tree = render(ui.ControlCenter, hooks, props);
  const showMore = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Show more alerts"))[0];
  assert.ok(showMore);
  const loading = showMore.props.onClick();
  const repeatedClick = showMore.props.onClick();
  assert.equal(requests.length, 3, "one page request is in flight per list generation");
  assert.match(requests[2].url, /afterCreatedAt=/);
  assert.match(requests[2].url, new RegExp(`afterIntentId=${firstRows[99].intentId}`));
  await resolveJson(requests[2], { deliveryQueue: [firstRows[0],
    { ...row(100), status: "failed" }], hasMore: false });
  await loading;
  await repeatedClick;
  tree = render(ui.ControlCenter, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Review new destination")).length, 101);
  assert.ok(textOf(tree).indexOf("Retry scheduled") < textOf(tree).indexOf("Waiting for worker"));
  assert.equal(findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Show more alerts")).length, 0);
});

test("refresh discards a delivery page response started before refreshed rows", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "alerts" };
  const row = (index) => ({ intentId: index.toString(16).padStart(64, "0"),
    createdAt: new Date(Date.UTC(2026, 9, 18, 0, index)).toISOString(),
    status: "pending", kind: "finding", destination: "local_log", attemptCount: 0 });
  const oldRows = Array.from({ length: 100 }, (_, index) => row(index));
  const state = (rows, pending) => ({ documents: [], worker: { status: "unverified" },
    dailyOperations: { localAgentSources: [] }, notifications: [], deliveryAttempts: [],
    reviewQueue: [], deliveryQueue: rows, deliveryQueueCounts: { pending } });
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], state(oldRows, 102));
  let tree = render(ui.ControlCenter, hooks, props);
  const pageRequest = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Show more alerts"))[0].props.onClick();
  const refreshRequest = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Refresh delivery status"))[0].props.onClick();
  await resolveJson(requests[3], { setupToken: "setup-token" });
  await resolveJson(requests[4], state([row(102), ...oldRows.slice(0, 99)], 103));
  await refreshRequest;
  await resolveJson(requests[2], { deliveryQueue: [row(100), row(101)], hasMore: false });
  await pageRequest;
  tree = render(ui.ControlCenter, hooks, props);
  assert.ok(findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Show more alerts")).length,
  "the refreshed list still has three older alerts to page through");
});

test("testing an edited schedule preserves the reviewed form until it is saved", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule" };
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], {
    documents: [{ kind: "schedule", documentId: "daily", state: "active", revision: 1,
      payload: { intervalHours: 24, claudeRoot: "", codexRoot: "", analyzeOnly: true, runMonitor: false } }],
    worker: { status: "unverified" }, dailyOperations: { localAgentSources: [] },
    notifications: [], deliveryAttempts: [], reviewQueue: [],
  });
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  let tree = render(ui.ControlCenter, hooks, props);
  const interval = findAll(tree, (node) => node.type === "input" && node.props.type === "number")[0];
  interval.props.onChange({ target: { value: "12" } });
  tree = render(ui.ControlCenter, hooks, props);
  const running = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Run once and review"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).intervalHours, 12);
  assert.equal(JSON.parse(requests[2].options.body).includeApprovedEvaluations, true);
  await resolveJson(requests[2], { analysis: { status: "completed" }, monitor: null,
    scheduledEvaluation: { status: "idle" } });
  await running;
  tree = render(ui.ControlCenter, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.type === "number")[0].props.value, 12);
  assert.equal(findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Save recurring schedule"))[0].props.disabled, false);
  assert.equal(requests.length, 3);
  const saving = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Save recurring schedule"))[0].props.onClick();
  await resolveJson(requests[3], { revision: 2 });
  await resolveJson(requests[4], { setupToken: "setup-token" });
  await resolveJson(requests[5], { documents: [{ kind: "schedule", documentId: "daily",
    state: "active", revision: 2, payload: { analyzeOnly: true, claudeRoot: "", codexRoot: "",
      includeApprovedEvaluations: true, intervalHours: 12, runMonitor: false } }],
    worker: { status: "unverified" }, dailyOperations: { localAgentSources: [] },
    notifications: [], deliveryAttempts: [], reviewQueue: [] });
  await saving;
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  tree = render(ui.ControlCenter, hooks, props);
  assert.match(textOf(tree), /Cycle checks completed/);
  assert.doesNotMatch(textOf(tree), /selected checks did not complete/);
  assert.match(textOf(tree), /Start the worker for this saved plan/);
  assert.match(textOf(tree), /verdict-service --tenant-id/);
  assert.doesNotMatch(textOf(tree), /verdict-service --storage/);
  assert.match(textOf(tree), /same store and tenant ID as the dashboard/);
});

test("guided setup treats a recent worker cycle as verified without a startup prompt", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule",
    meta: { totalAgentRuns: 1 } };
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], {
    documents: [{ kind: "schedule", documentId: "daily", state: "active", revision: 2,
      payload: { intervalHours: 24, analyzeOnly: true, runMonitor: false } }],
    worker: { status: "recent" }, lastCycle: { state: "completed", trigger: "worker",
      scheduleRevision: 2, finishedAt: "2026-10-19T00:00:00Z" },
    dailyOperations: { localAgentSources: [] }, notifications: [], deliveryAttempts: [],
    reviewQueue: [],
  });
  const summary = textOf(render(ui.ControlCenter, hooks, props));
  assert.match(summary, /Recurring checks ran on current plan/);
  assert.doesNotMatch(summary, /Start the worker for this saved plan/);
});

test("guided plan never treats observed source labels as filesystem roots", async () => {
  const ui = await loadUiModule();
  const initiallyObserved = render(ui.ControlCenter, createEffectHooks(), {
    configUrl: "/api/config", section: "schedule", meta: { totalAgentRuns: 2,
      agentRunSources: [{ sourceKind: "claude-code", runs: 1 },
        { sourceKind: "codex", runs: 1 }] },
  });
  assert.equal(findAll(initiallyObserved, (node) => node.type === "input"
    && ["~/.claude/projects", "~/.codex/sessions"].includes(node.props.value)).length, 0);
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule", meta: {
    totalAgentRuns: 2, agentRunSources: [] } };
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  await resolveJson(requests[1], { documents: [], worker: {}, dailyOperations: {},
    notifications: [], deliveryAttempts: [], reviewQueue: [] });
  let tree = render(ui.ControlCenter, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.value === "~/.claude/projects").length, 0);
  const loaded = { ...props, meta: { totalAgentRuns: 2,
    agentRunSources: [{ sourceKind: "claude-code", runs: 1 }, { sourceKind: "codex", runs: 1 }] } };
  render(ui.ControlCenter, hooks, loaded);
  hooks.flushEffects();
  tree = render(ui.ControlCenter, hooks, loaded);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.value === "~/.claude/projects").length, 0);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.value === "~/.codex/sessions").length, 0);
  assert.match(textOf(tree), /Already captured activity only/);
  const run = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Run once and review"))[0];
  const running = run.props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).claudeRoot, undefined);
  assert.equal(JSON.parse(requests[2].options.body).codexRoot, undefined);
  await resolveJson(requests[2], { analysis: { status: "completed" }, monitor: null,
    scheduledEvaluation: { status: "disabled_by_plan" } });
  await running;
  tree = render(ui.ControlCenter, hooks, loaded);
  const codexInput = findAll(tree, (node) => node.type === "input"
    && node.props.placeholder === "~/.codex/sessions")[0];
  codexInput.props.onChange({ target: { value: "/tmp/explicit-synthetic-history" } });
  tree = render(ui.ControlCenter, hooks, loaded);
  const rescan = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Run once and review"))[0].props.onClick();
  assert.equal(JSON.parse(requests[3].options.body).codexRoot, "/tmp/explicit-synthetic-history");
  await resolveJson(requests[3], { analysis: { status: "completed" }, monitor: null,
    scheduledEvaluation: { status: "disabled_by_plan" } });
  await rescan;
});

test("guided plan retains a saved explicit rescan path", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule", meta: {
    totalAgentRuns: 1, agentRunSources: [{ sourceKind: "codex", runs: 1 }] } };
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  await resolveJson(requests[1], { documents: [{ kind: "schedule", documentId: "daily",
    state: "active", revision: 1, payload: { intervalHours: 24, analyzeOnly: true,
      codexRoot: "/tmp/saved-synthetic-history", runMonitor: false } }], worker: {},
    dailyOperations: {}, notifications: [], deliveryAttempts: [], reviewQueue: [] });
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  const tree = render(ui.ControlCenter, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input"
    && node.props.value === "/tmp/saved-synthetic-history").length, 1);
});

test("empty plan directs capture before a no-evidence check", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const destinations = [];
  const props = { configUrl: "/api/config", section: "schedule",
    meta: { totalAgentRuns: 0, monitorEligibleCalls: 0 },
    onNavigate: (target) => destinations.push(target) };
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  await resolveJson(requests[1], { documents: [], worker: {}, dailyOperations: {},
    notifications: [], deliveryAttempts: [], reviewQueue: [] });
  const tree = render(ui.ControlCenter, hooks, props);
  assert.match(textOf(tree), /Capture evidence before running a check/);
  assert.doesNotMatch(textOf(tree), /Review recorded execution issues|Already captured activity only/);
  const run = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Run once and review"))[0];
  assert.equal(run.props.disabled, true);
  const capture = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Set up capture"))[0];
  capture.props.onClick();
  assert.deepEqual(destinations, [{ tab: "settings", section: "sources" }]);
});

test("guided setup reviews an AI agent-check draft without saving or starting it", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule",
    meta: { totalAgentRuns: 1, monitorEligibleCalls: 0,
      agentRunSources: [{ sourceKind: "claude-code", runs: 1 }] } };
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  await resolveJson(requests[1], { documents: [], worker: {}, dailyOperations: {},
    notifications: [], deliveryAttempts: [], reviewQueue: [] });
  let tree = render(ui.ControlCenter, hooks, props);
  const assist = findAll(tree, (node) => node.type?.name === "EvidenceExplain")[0];
  assert.ok(assist);
  assist.props.onApplyDraft({ check: "agent_runs", grouping: "none", intervalHours: 12,
    why: "Review new agent activity." });
  tree = render(ui.ControlCenter, hooks, props);
  assert.match(textOf(tree), /AI draft filled the local check interval/);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.type === "number")[0].props.value, 12);
  assert.equal(requests.length, 2);
});

test("pause keeps legacy judge consent untouched and resume requires a newly reviewed no-egress cycle", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule" };
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  const legacy = { kind: "schedule", documentId: "daily", state: "active", revision: 1,
    payload: { intervalHours: 24, analyzeOnly: true, runMonitor: false } };
  await resolveJson(requests[1], { documents: [legacy], worker: { status: "recent" },
    dailyOperations: {}, notifications: [], deliveryAttempts: [], reviewQueue: [] });
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  let tree = render(ui.ControlCenter, hooks, props);
  const pause = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Pause future checks"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).state, "disabled");
  assert.equal(JSON.parse(requests[2].options.body).payload.includeApprovedEvaluations, undefined);
  await resolveJson(requests[2], { revision: 2 });
  await resolveJson(requests[3], { setupToken: "token" });
  await resolveJson(requests[4], { documents: [{ ...legacy, state: "disabled", revision: 2 }],
    worker: { status: "recent" }, dailyOperations: {}, notifications: [],
    deliveryAttempts: [], reviewQueue: [] });
  await pause;
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  tree = render(ui.ControlCenter, hooks, props);
  assert.match(textOf(tree), /Plan paused/);
  assert.match(textOf(tree), /Older plan did not record a judge choice/);
  assert.equal(findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Resume recurring checks"))[0].props.disabled, true);
  const run = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Run once and review"))[0].props.onClick();
  assert.equal(JSON.parse(requests[5].options.body).includeApprovedEvaluations, false);
  await resolveJson(requests[5], { analysis: { status: "completed" }, monitor: null,
    scheduledEvaluation: { status: "disabled_by_plan" } });
  await run;
  tree = render(ui.ControlCenter, hooks, props);
  const resume = findAll(tree, (node) => node.type?.name === "Button"
    && textOf(node).includes("Resume recurring checks"))[0];
  assert.equal(resume.props.disabled, false);
  resume.props.onClick();
  assert.equal(JSON.parse(requests[6].options.body).state, "active");
  assert.equal(JSON.parse(requests[6].options.body).payload.includeApprovedEvaluations, false);
});

test("recent heartbeat does not imply the newly saved revision has completed", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", section: "schedule" };
  render(ui.ControlCenter, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  await resolveJson(requests[1], { documents: [{ kind: "schedule", documentId: "daily",
    state: "active", revision: 2, payload: { analyzeOnly: true, intervalHours: 24,
      includeApprovedEvaluations: false } }], worker: { status: "recent" },
    lastCycle: { state: "completed", trigger: "manual" }, dailyOperations: {},
    notifications: [], deliveryAttempts: [], reviewQueue: [] });
  const tree = render(ui.ControlCenter, hooks, props);
  assert.match(textOf(tree), /Worker seen; current plan check pending/);
});

test("Monitor compares conversation grades and detects changed example evidence", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", view: "history" };
  render(ui.Monitor, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { state: "not_configured", active: null, candidate: null,
    agentEvaluators: [] });
  let tree = render(ui.Monitor, hooks, props);
  findAll(tree, (node) => node.type === "select" && findAll(node,
    (child) => child.type === "option" && child.props.value === "conversation").length)[0]
    .props.onChange({ target: { value: "conversation" } });
  tree = render(ui.Monitor, hooks, props);
  const field = (placeholder) => findAll(tree, (node) => node.type === "input" &&
    node.props.placeholder?.includes(placeholder))[0];
  field("64-character").props.onChange({ target: { value: "a".repeat(64) } });
  field("e.g. quality").props.onChange({ target: { value: "quality" } });
  field("e.g. group").props.onChange({ target: { value: "group" } });
  const dates = findAll(tree, (node) => node.type === "input" && node.props.type === "datetime-local");
  ["2026-09-01T00:00", "2026-09-03T00:00", "2026-09-03T00:00", "2026-09-05T00:00"]
    .forEach((value, index) => dates[index].props.onChange({ target: { value } }));
  tree = render(ui.Monitor, hooks, props);
  const pending = findAll(tree, (node) => node.type === "button" &&
    textOf(node) === "Run historical comparison")[0].props.onClick();
  const submitted = JSON.parse(requests[2].options.body);
  assert.deepEqual(Object.keys(submitted).sort(), ["analysisUnit", "currentEnd", "currentStart",
    "dimension", "evaluatorFingerprint", "labelKey", "referenceEnd", "referenceStart"].sort());
  assert.equal(submitted.dimension, "quality");
  assert.equal(submitted.labelKey, "group");
  const empty = { pass: [], fail: [], unclear: [], error: [], ungraded: [], ineligible: [] };
  const base = { captured: 2, eligible: 2, ineligible: 0, pass: 1, fail: 0, unclear: 0, error: 1, ungraded: 0,
    evaluable: 1, passRate: 1, interval: [0.2, 1],
    examples: { ...empty, pass: [{ id: "b".repeat(32), revision: "old", qualityState: "pass" }],
      error: [{ id: "d".repeat(32), revision: "same", qualityState: "error" }] } };
  const current = { ...base, pass: 0, fail: 1, passRate: 0, interval: [0, 0.8],
    examples: { ...empty, fail: [{ id: "c".repeat(32), revision: "current", qualityState: "fail" }] } };
  await resolveJson(requests[2], { state: "descriptive", unit: "conversation",
    method: "current_snapshot_binary_v1", evaluatorFingerprint: "a".repeat(64),
    dimension: "quality", labelKey: "group",
    referenceStart: submitted.referenceStart, referenceEnd: submitted.referenceEnd,
    currentStart: submitted.currentStart, currentEnd: submitted.currentEnd,
    reference: base, current, effect: -1, gradeEvidenceCount: 3, groups: [] });
  await pending;
  tree = render(ui.Monitor, hooks, props);
  const preview = findAll(tree, (node) => node.type?.name === "ConversationPreview")[0];
  assert.ok(preview);
  const rendered = render(preview.type, createHooks(), preview.props);
  assert.match(textOf(rendered), /does not create an alert/);
  const noGrades = render(preview.type, createHooks(), { ...preview.props,
    preview: { ...preview.props.preview, gradeEvidenceCount: 0 } });
  assert.match(textOf(noGrades), /Check the fingerprint and dates/);
  const rate = findAll(rendered, (node) => node.type?.name === "ConversationRate")[0];
  const rateTree = render(rate.type, createHooks(), rate.props);
  findAll(rateTree, (node) => node.type === "button")[0].props.onClick();
  await resolveJson(requests[3], { conversation: { revision: "old", messages: [
    { role: "user", content: "Question." }, { role: "assistant", content: "Answer." },
  ] }, assessments: [{ status: "completed", dimensions: { quality: { state: "pass" } } }] });
  tree = render(ui.Monitor, hooks, props);
  assert.match(textOf(tree), /Answer\./);
  findAll(rateTree, (node) => node.type === "button")[0].props.onClick();
  await resolveJson(requests[4], { conversation: { revision: "new", messages: [
    { role: "user", content: "Question." }, { role: "assistant", content: "Changed answer." },
  ] }, assessments: [] });
  tree = render(ui.Monitor, hooks, props);
  assert.match(textOf(tree), /transcript changed since the comparison/);
  findAll(rateTree, (node) => node.type === "button")[1].props.onClick();
  await resolveJson(requests[5], { conversation: { revision: "same", messages: [
    { role: "user", content: "Question." }, { role: "assistant", content: "Answer." },
  ] }, assessments: [{ status: "completed", target_position: null,
    dimensions: { quality: { state: "pass" } } }] });
  tree = render(ui.Monitor, hooks, props);
  assert.match(textOf(tree), /grade changed since the comparison/);
  assert.doesNotMatch(textOf(tree), /Changed answer\./);
});

test("conversation evaluation requires a supported provider after Jev selection", async () => {
  const ui = await loadUiModule();
  const selected = [];
  const tree = render(ui.ConversationEvaluation, createHooks(), {
    root: "", token: "setup-token", provider: "jev", model: "jev-1.13.0",
    providerState: { configured: true }, updatePreferences: () => {},
    changeProvider: (next) => selected.push(next),
  });
  assert.match(textOf(tree), /Jev cannot grade conversations/);
  const preview = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview first page"))[0];
  assert.equal(preview.props.disabled, true);
  const provider = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "jev").length)[0];
  provider.props.onChange({ target: { value: "openai" } });
  assert.deepEqual(selected, ["openai"]);
});

test("switching from Jev to a conversation provider selects its model explicitly", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const props = { configUrl: "/api/config" };
  let tree = render(ui.EvaluatorLab, hooks, props);
  const provider = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "jev").length)[0];
  provider.props.onChange({ target: { value: "jev" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const unit = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "conversation").length)[0];
  unit.props.onChange({ target: { value: "conversation" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  let conversation = findAll(tree, (node) => node.type === ui.ConversationEvaluation)[0];
  assert.equal(conversation.props.provider, "jev");
  conversation.props.changeProvider("openai");
  tree = render(ui.EvaluatorLab, hooks, props);
  conversation = findAll(tree, (node) => node.type === ui.ConversationEvaluation)[0];
  assert.equal(conversation.props.provider, "openai");
  assert.equal(conversation.props.model, "gpt-4o-mini");
});

test("switching directly from Anthropic to Google selects its current default", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const props = { configUrl: "/api/config" };
  const field = (tree, label, type) => findAll(
    findAll(tree, (node) => node.type === "label" && textOf(node).startsWith(label))[0],
    (node) => node.type === type,
  )[0];
  let tree = render(ui.EvaluatorLab, hooks, props);
  assert.equal(field(tree, "Model", "input").props.value, "claude-haiku-4-5");
  field(tree, "Provider", "select").props.onChange({ target: { value: "google" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.equal(field(tree, "Model", "input").props.value, "gemini-3.5-flash-lite");
});

test("Evaluator Lab selects Jev and runs only the approved preview", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config" };
  render(ui.EvaluatorLab, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { evalPackageAvailable: true,
    providers: [{ provider: "jev", configured: true, sdkAvailable: true,
      secretReference: "TYPESAFE_API_KEY" }] });

  let tree = render(ui.EvaluatorLab, hooks, props);
  const provider = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "jev").length)[0];
  assert.ok(provider);
  provider.props.onChange({ target: { value: "jev" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const model = findAll(tree, (node) => node.type === "label" && textOf(node).startsWith("Model"))[0];
  const modelInput = findAll(model, (node) => node.type === "input")[0];
  assert.equal(modelInput.props.value, "jev-1.13.0");
  assert.notEqual(modelInput.props.disabled, true);
  modelInput.props.onChange({ target: { value: "jev-1.14.0" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /TYPESAFE_API_KEY/);

  const preview = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview eligible calls"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).provider, "jev");
  assert.equal(JSON.parse(requests[2].options.body).model, "jev-1.14.0");
  await resolveJson(requests[2], {
    unit: "trace", availableTraces: 1, eligible: 1, alreadyJudged: 0,
    notEvaluable: 0, notEvaluableReasons: {}, plannedCalls: 1,
    destination: "https://judge-a.example",
    estimatedMaximumCostUsd: null, maximumOutputTokens: null,
    rubric: { dimensions: ["relevance"], skippedDimensions: [] },
    planFingerprint: "jev-plan", plannedTraces: [{ traceId: "trace-1",
      evidenceFingerprint: "a".repeat(64) }],
  });
  await preview;
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /Provider pricing unavailable/);
  assert.match(textOf(tree), /https:\/\/judge-a\.example/);
  assert.doesNotMatch(textOf(tree), /512-token output allowance/);
  const changedModel = findAll(tree, (node) => node.type === "label"
    && textOf(node).startsWith("Model"))[0];
  findAll(changedModel, (node) => node.type === "input")[0].props.onChange({
    target: { value: "jev-1.15.0" },
  });
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run 1 judge calls"))[0].props.disabled, true);
  findAll(changedModel, (node) => node.type === "input")[0].props.onChange({
    target: { value: "jev-1.14.0" },
  });
  tree = render(ui.EvaluatorLab, hooks, props);
  const consent = findAll(tree, (node) => node.type === "input" &&
    node.props.type === "checkbox").at(-1);
  consent.props.onChange({ target: { checked: true } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const run = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run 1 judge calls"))[0].props.onClick();
  const approved = JSON.parse(requests[3].options.body);
  assert.equal(approved.provider, "jev");
  assert.equal(approved.model, "jev-1.14.0");
  assert.equal(approved.planFingerprint, "jev-plan");
  assert.equal(approved.confirmExternalEgress, true);
  await resolveJson(requests[3], { unit: "trace", availableTraces: 1,
    eligible: 1, completed: 1, alreadyJudged: 0, errors: 0,
    notEvaluable: 0, notEvaluableReasons: {}, evaluatorFingerprint: "a".repeat(64) });
  await run;
  assert.match(textOf(render(ui.EvaluatorLab, hooks, props)), /Evaluation completed/);
});

test("Evaluator Lab calibration discloses unredacted text and binds the approved endpoint", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config" };
  render(ui.EvaluatorLab, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { evalPackageAvailable: true,
    providers: [{ provider: "jev", configured: true, sdkAvailable: true,
      secretReference: "TYPESAFE_API_KEY" }] });

  let tree = render(ui.EvaluatorLab, hooks, props);
  const provider = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "jev").length)[0];
  provider.props.onChange({ target: { value: "jev" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const path = findAll(tree, (node) => node.type === "input" &&
    node.props.placeholder === "/path/to/labels.jsonl")[0];
  path.props.onChange({ target: { value: "/tmp/labels.jsonl" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const preview = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview label set"))[0].props.onClick();
  await resolveJson(requests[2], { setName: "labels", examples: 1, plannedCalls: 1,
    labelCounts: { relevance: 1 }, destination: "https://judge-a.example",
    planFingerprint: "calibration-plan" });
  await preview;

  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /without Verdict redaction/);
  assert.match(textOf(tree), /https:\/\/judge-a\.example/);
  const changedPath = findAll(tree, (node) => node.type === "input" &&
    node.props.placeholder === "/path/to/labels.jsonl")[0];
  changedPath.props.onChange({ target: { value: "/tmp/other-labels.jsonl" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run calibration"))[0].props.disabled, true);
  changedPath.props.onChange({ target: { value: "/tmp/labels.jsonl" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const consent = findAll(tree, (node) => node.type === "input" &&
    node.props.type === "checkbox").at(-1);
  consent.props.onChange({ target: { checked: true } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const run = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run calibration"))[0].props.onClick();
  const approved = JSON.parse(requests[3].options.body);
  assert.equal(approved.planFingerprint, "calibration-plan");
  assert.equal(approved.labelSetPath, "/tmp/labels.jsonl");
  await resolveJson(requests[3], { status: "insufficient_data", exampleAgreement: 1,
    totalExamples: 1, errors: 0 });
  await run;
});

test("Evaluator Lab previews a native Turn page and sends its approved identities", async () => {
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
  const unit = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "agent_turn").length)[0];
  unit.props.onChange({ target: { value: "agent_turn" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const preview = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview eligible calls"))[0].props.onClick();
  const sent = JSON.parse(requests[2].options.body);
  assert.equal(sent.unit, "agent_turn");
  assert.equal(sent.scanLimit, 100);
  await resolveJson(requests[2], {
    unit: "agent_turn", availableTurns: 1, eligible: 1, alreadyJudged: 0,
    notEvaluable: 0, plannedCalls: 1, estimatedMaximumCostUsd: 0.01,
    maximumOutputTokens: 512, notEvaluableReasons: {},
    rubric: { dimensions: ["relevance"], skippedDimensions: [] },
    planFingerprint: "turn-plan", plannedTurns: [{ runId: "run", turnId: "turn", evidenceFingerprint: "a".repeat(64) }],
    hasMore: false,
  });
  await preview;
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /1\s+tenant-visible candidates/);
  assert.match(textOf(tree), /provider billing can exceed it/);
  assert.match(textOf(tree), /cost is an estimate, not a billing cap/);
  assert.doesNotMatch(textOf(tree), /maximum estimated cost/i);
  const consent = findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox").at(-1);
  consent.props.onChange({ target: { checked: true } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const run = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run 1 judge calls"))[0].props.onClick();
  const approved = JSON.parse(requests[3].options.body);
  assert.deepEqual(approved.plannedTurns, [{ runId: "run", turnId: "turn", evidenceFingerprint: "a".repeat(64) }]);
  assert.equal(approved.plannedTraces, undefined);
  await resolveJson(requests[3], { unit: "agent_turn", availableTurns: 1,
    eligible: 1, completed: 0, alreadyJudged: 0, errors: 0, stale: 0, inProgress: 1,
    notEvaluable: 0, notEvaluableReasons: {}, evaluatorFingerprint: "a".repeat(64) });
  await run;
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /Current Turn scores appear on the Agent Runs detail page/);
  assert.equal(findAll(tree, (node) => node.props?.label === "Judge busy; retry" &&
    node.props.value === 1).length, 1);
  assert.doesNotMatch(textOf(tree), /View evaluated traces/);
});

test("Turn tool-count consent binds the displayed preview and disables stale execution", async () => {
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
  const unit = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "agent_turn").length)[0];
  unit.props.onChange({ target: { value: "agent_turn" } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const toolOption = findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0];
  toolOption.props.onChange({ target: { checked: true } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const preview = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview eligible calls"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).toolEvidence, "counts_v1");
  await resolveJson(requests[2], {
    unit: "agent_turn", toolEvidence: "counts_v1", availableTurns: 1,
    eligible: 1, alreadyJudged: 0, notEvaluable: 0, plannedCalls: 1,
    estimatedMaximumCostUsd: 0.01, maximumOutputTokens: 512,
    notEvaluableReasons: {}, rubric: { dimensions: ["relevance"], skippedDimensions: [] },
    planFingerprint: "tool-plan", plannedTurns: [{ runId: "run", turnId: "turn",
      evidenceFingerprint: "b".repeat(64) }], hasMore: false,
  });
  await preview;
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /recorded tool-event counts/);
  assert.match(textOf(tree), /no downstream or source verification/);
  const consent = findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox").at(-1);
  consent.props.onChange({ target: { checked: true } });
  tree = render(ui.EvaluatorLab, hooks, props);
  const run = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run 1 judge calls"))[0];
  assert.equal(run.props.disabled, false);
  const countToggle = findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0];
  countToggle.props.onChange({ target: { checked: false } });
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /Configuration changed/);
  assert.equal(findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run 1 judge calls"))[0].props.disabled, true);
});

test("Evaluator Lab restores judge choices after reload without restoring approval", async () => {
  const ui = await loadUiModule();
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const saved = new Map();
  const requests = [];
  globalThis.window = { sessionStorage: {
    getItem: (key) => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
  } };
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, method: options.method || "GET" });
    const data = url.endsWith("/api/setup/token") ? { setupToken: "token" }
      : url.endsWith("/api/evaluators") ? { evalPackageAvailable: true,
        providers: [{ provider: "google", configured: true, secretReference: "GOOGLE_API_KEY" }] }
        : { unit: "agent_turn", toolEvidence: "counts_v1", availableTurns: 1,
          eligible: 1, alreadyJudged: 0, notEvaluable: 0, plannedCalls: 1,
          estimatedMaximumCostUsd: 0.001, maximumOutputTokens: 512,
          notEvaluableReasons: {}, planFingerprint: "plan",
          plannedTurns: [{ runId: "run", turnId: "turn", evidenceFingerprint: "a".repeat(64) }],
          hasMore: false };
    return { ok: true, json: async () => data };
  };
  const props = { configUrl: "/api/config" };
  const field = (tree, labelText, type) => {
    const label = findAll(tree, (node) => node.type === "label"
      && node.props.children[0] === labelText)[0];
    return findAll(label, (node) => node.type === type)[0];
  };
  try {
    const first = createEffectHooks();
    let tree = render(ui.EvaluatorLab, first, props);
    first.flushEffects();
    await new Promise(setImmediate);
    tree = render(ui.EvaluatorLab, first, props);
    field(tree, "Evaluation unit", "select").props.onChange({ target: { value: "agent_turn" } });
    tree = render(ui.EvaluatorLab, first, props);
    findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
      .props.onChange({ target: { checked: true } });
    field(tree, "Provider", "select").props.onChange({ target: { value: "google" } });
    field(tree, "Model", "input").props.onChange({ target: { value: "gemini-2.5-flash-lite" } });
    field(tree, "Evaluation scope", "select").props.onChange({ target: { value: "limit" } });
    tree = render(ui.EvaluatorLab, first, props);
    field(tree, "Evaluation scope", "input").props.onChange({ target: { value: "10" } });
    tree = render(ui.EvaluatorLab, first, props);
    first.flushEffects();
    await findAll(tree, (node) => node.type === "button"
      && textOf(node).includes("Preview eligible calls"))[0].props.onClick();
    tree = render(ui.EvaluatorLab, first, props);
    findAll(tree, (node) => node.type === "input"
      && node.props.type === "checkbox").at(-1).props.onChange({ target: { checked: true } });

    const second = createEffectHooks();
    tree = render(ui.EvaluatorLab, second, props);
    second.flushEffects();
    await new Promise(setImmediate);
    tree = render(ui.EvaluatorLab, second, props);
    assert.equal(field(tree, "Evaluation unit", "select").props.value, "agent_turn");
    assert.equal(findAll(tree, (node) => node.type === "input"
      && node.props.type === "checkbox")[0].props.checked, true);
    assert.equal(field(tree, "Provider", "select").props.value, "google");
    assert.equal(field(tree, "Model", "input").props.value, "gemini-2.5-flash-lite");
    assert.equal(field(tree, "Evaluation scope", "select").props.value, "limit");
    assert.equal(field(tree, "Evaluation scope", "input").props.value, 10);
    assert.doesNotMatch(textOf(tree), /Planned calls|Run 1 judge calls/);
    assert.deepEqual(requests.filter((request) => request.method === "POST")
      .map((request) => request.url), ["/api/evaluators/preview"]);
    const persisted = JSON.parse([...saved.values()][0]);
    assert.deepEqual(Object.keys(persisted).sort(),
      ["includeToolCounts", "judgeAll", "maxCalls", "model", "provider", "unit"]);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("Evaluator Lab bounds saved choices and tolerates unavailable browser storage", async () => {
  const ui = await loadUiModule();
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
  const props = { configUrl: "/api/config" };
  try {
    for (const [stored, provider, model, unit, toolCounts, scope, cap] of [
      ["{invalid", "anthropic", "claude-haiku-4-5", "trace", false, "all", null],
      [JSON.stringify({ provider: "unknown", model: "", unit: "unknown",
        includeToolCounts: "true", judgeAll: "limit", maxCalls: 10001 }),
        "anthropic", "claude-haiku-4-5", "trace", false, "all", null],
      [JSON.stringify({ provider: "google", model: "😀".repeat(100), unit: "agent_turn",
        includeToolCounts: true, judgeAll: false, maxCalls: 10 }),
        "google", "claude-haiku-4-5", "agent_turn", true, "limit", 10],
      [JSON.stringify({ provider: "openai", model: "local/cheap-model", judgeAll: false, maxCalls: -1 }),
        "openai", "local/cheap-model", "trace", false, "limit", 100],
      [JSON.stringify({ provider: "jev", model: "jev-1.14.0" }),
        "jev", "jev-1.14.0", "trace", false, "all", null],
      [JSON.stringify({ provider: "jev", model: "" }),
        "jev", "jev-1.13.0", "trace", false, "all", null],
      ["x".repeat(2000), "anthropic", "claude-haiku-4-5", "trace", false, "all", null],
    ]) {
      globalThis.window = { sessionStorage: { getItem: () => stored, setItem() {} } };
      const tree = render(ui.EvaluatorLab, createHooks(), props);
      const labels = findAll(tree, (node) => node.type === "label");
      const control = (name, type) => findAll(labels.find((node) => node.props.children[0] === name),
        (node) => node.type === type)[0];
      assert.equal(control("Provider", "select").props.value, provider);
      assert.equal(control("Model", "input").props.value, model);
      assert.equal(control("Evaluation unit", "select").props.value, unit);
      if (unit === "agent_turn") assert.equal(findAll(tree, (node) => node.type === "input"
        && node.props.type === "checkbox")[0].props.checked, toolCounts);
      assert.equal(control("Evaluation scope", "select").props.value, scope);
      if (cap !== null) assert.equal(control("Evaluation scope", "input").props.value, cap);
    }
    globalThis.window = { get sessionStorage() { throw new Error("blocked"); } };
    const hooks = createEffectHooks();
    const tree = render(ui.EvaluatorLab, hooks, props);
    assert.equal(findAll(tree, (node) => node.type === "select" && node.props.value === "anthropic").length, 1);
    assert.doesNotThrow(() => hooks.flushEffects());
    await new Promise(setImmediate);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
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
    (node) => node.type === "button" && textOf(node).includes("Preview eligible calls"),
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

function bundle(evaluator, samples = [], reportDays = 30) {
  return {
    meta: { totalTraces: samples.length, totalJudged: 0, workload: null },
    managementReport: {
      schema: "management-report-v1",
      period: { days: reportDays, startDate: "2026-08-13", endDate: "2026-09-11" },
      scope: {
        firstCapturedAt: "2026-09-08T00:00:00+00:00",
        latestCapturedAt: "2026-09-10T00:03:00+00:00",
        calls: 28, successfulCalls: 27, failedCalls: 1, successRatePct: 96.4,
        inputTokens: 2800, outputTokens: 700, totalTokens: 3500,
        tokenKnownCalls: 28, cachedInputTokens: 2240, uncachedInputTokens: 560,
        inputBreakdownKnownCalls: 28, costUsd: 0.028, costKnownCalls: 27,
        averageLatencyMs: 1000, latencyKnownCalls: 26,
        latencySampledCalls: 26, p50LatencyMs: 900, p95LatencyMs: 1800,
        identifiedApplications: 2, unattributedCalls: 0,
        judgedCalls: 20, judgeErrorCalls: 1,
      },
      timeline: { availableDates: 2, shownDates: 2, rows: [
        { date: "2026-09-08", calls: 24, totalTokens: 3000, tokenKnownCalls: 24 },
        { date: "2026-09-10", calls: 4, totalTokens: 500, tokenKnownCalls: 4 },
      ] },
      applications: { availableRows: 2, shownRows: 2, rows: [
        { name: "orders-api", environment: "production", attributed: true, calls: 24, successfulCalls: 24, failedCalls: 0, successRatePct: 100, inputTokens: 2400, outputTokens: 600, totalTokens: 3000, tokenKnownCalls: 24, cachedInputTokens: 1920, uncachedInputTokens: 480, inputBreakdownKnownCalls: 24, costUsd: 0.024, costKnownCalls: 24, averageLatencyMs: 900, latencyKnownCalls: 24 },
        { name: "billing-worker", environment: "staging", attributed: true, calls: 4, successfulCalls: 3, failedCalls: 1, successRatePct: 75, inputTokens: 400, outputTokens: 100, totalTokens: 500, tokenKnownCalls: 4, cachedInputTokens: 320, uncachedInputTokens: 80, inputBreakdownKnownCalls: 4, costUsd: 0.004, costKnownCalls: 3, averageLatencyMs: 1600, latencyKnownCalls: 2 },
      ] },
      models: { availableRows: 2, shownRows: 2, rows: [
        { provider: "openai", model: "gpt-5-mini", calls: 24, successfulCalls: 24, failedCalls: 0, successRatePct: 100, inputTokens: 2400, outputTokens: 600, totalTokens: 3000, tokenKnownCalls: 24, cachedInputTokens: 1920, uncachedInputTokens: 480, inputBreakdownKnownCalls: 24, costUsd: 0.024, costKnownCalls: 24, averageLatencyMs: 900, latencyKnownCalls: 24 },
        { provider: "custom-provider", model: "custom-model-v1", calls: 4, successfulCalls: 3, failedCalls: 1, successRatePct: 75, inputTokens: 400, outputTokens: 100, totalTokens: 500, tokenKnownCalls: 4, cachedInputTokens: 320, uncachedInputTokens: 80, inputBreakdownKnownCalls: 4, costUsd: 0.004, costKnownCalls: 3, averageLatencyMs: 1600, latencyKnownCalls: 2 },
      ] },
    },
    evaluation: { selectedId: evaluator, availableIdentities: [] },
    clusterHealth: { status: "empty", messages: [], minSampleSize: 30, clustersMeetingSampleFloor: 0, nClusters: 0 },
    providers: [], clusters: [], dimensionOverall: [], tsRows: [],
    passrate: [], clusterPassrate: [], haikuDim: [], samples,
    providerDimension: [], evaluatorHealth: [], scoreCoverage: {},
    truncation: {
      applied: samples.length > 30,
      resources: { traceSamples: { available: samples.length, shown: Math.min(samples.length, 30), limit: 30 } },
    },
  };
}

function insightsSnapshot(status = "error") {
  return {
    schema: "agent-insights-v2",
    scope: {
      availableRuns: 0, analyzedRuns: 0, complete: true,
      traces: { available: 0, analyzed: 0, complete: true },
    },
    findings: [],
    dataHealth: {
      counts: { runs: 0, turns: 0, events: 0 },
      eventTypes: {}, eventStatuses: {}, promptStates: {}, responseStates: {},
      traceLinks: { modelCalls: 0, linked: 0, unlinked: 0 },
      traceEvidence: {
        promptPresent: 0, responsePresent: 0, judgeEligible: 0,
        notEvaluable: 0, notEvaluableReasons: {},
      },
      traceOperations: {}, traceFinishReasons: {},
    },
    reliability: {
      runOutcomes: {}, turnOutcomes: {}, traceOutcomes: {},
      toolErrors: 0, commandFailures: 0, testFailures: 0, retries: 0,
    },
    performance: {
      modelCalls: 0, toolCalls: 0, inputTokens: null, outputTokens: null,
      averageModelLatencyMs: null, latencyKnownCalls: 0,
      costUsd: null, costState: "not_captured",
    },
    behavior: {
      findingRuns: 0, findingTypes: 0, capturedResponses: 0,
      averageResponseCharacters: null, refusals: 0,
      apologyStarts: 0, hedges: 0, validJsonResponses: 0,
    },
    sourceActivity: [], modelComparisons: [],
    error: status === "error" ? { code: "analysis_failed" } : undefined,
    analysisState: {
      status, analysisId: "analysis-1", analyzerVersion: "agent-insights-v2",
      cutoff: "2026-09-17T00:00:00Z", completedAt: "2026-09-17T00:00:01Z",
      inputFingerprint: "a".repeat(64),
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

test("usage chart drills from day to session and explains only captured numbers", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage", onOpenRun: () => {} };
  const base = {
    window: { complete: true, turnsComplete: true },
    days: [
      { day: "2026-10-07", turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 1, listPriceUsd: 0.01 },
      { day: "2026-10-08", turns: 2, tokenKnownTurns: 1, totalTokens: 300, unattributedTokens: 300, pricedTurns: 1, listPriceUsd: 0.02 },
    ],
    selectedDay: "2026-10-08", dayModels: [], sessions: [],
    sessionsTruncated: false, selectedSession: null,
  };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  assert.equal(requests[0].url, "/api/usage");
  await resolveJson(requests[0], base);
  let tree = render(ui.Usage, hooks, props);
  const modelUnknown = findAll(tree, (node) => node.type?.name === "Fact"
    && node.props.label === "Tokens without one recorded model")[0];
  assert.ok(modelUnknown);
  assert.equal(modelUnknown.props.value, "300");
  assert.match(modelUnknown.props.detail, /no model name or multiple model names/);
  const chart = findAll(tree, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
  findAll(chartTree, (node) => node.type === "button" && node.props["aria-label"]?.includes("2026-10-07"))[0].props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  assert.equal(requests[1].url, "/api/usage?day=2026-10-07");
  await resolveJson(requests[1], { ...base, selectedDay: "2026-10-07", sessions: [{
    sessionId: "session-a", agentName: "codex", sourceKind: "codex", runIds: ["run-a"],
    turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 1, listPriceUsd: 0.01,
  }] });
  tree = render(ui.Usage, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("run"))[0].props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  assert.equal(requests[2].url, "/api/usage?day=2026-10-07&session_id=session-a&source_kind=codex");
  await resolveJson(requests[2], { ...base, selectedDay: "2026-10-07", sessions: [{
    sessionId: "session-a", agentName: "codex", sourceKind: "codex", runIds: ["run-a"],
    turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 1, listPriceUsd: 0.01,
  }], selectedSession: {
    sessionId: "session-a", runIds: ["run-a"], turnCount: 1,
    totalTokens: 100, tokenKnownTurns: 1, pricedTurns: 1, listPriceUsd: 0.01,
    inputTokens: 80, outputTokens: 20, cachedInputTokens: 0, cacheWriteInputTokens: 0,
    componentCoverage: { inputTokens: 1, outputTokens: 1, cachedInputTokens: 1, cacheWriteInputTokens: 1 },
    elapsedSpanSeconds: 20, selectedDayTokens: 100,
    topTurns: [{ turnId: "turn-a", runId: "run-a", sequence: 0, totalTokens: 100 }],
    turns: [{ turnId: "turn-a", runId: "run-a", sequence: 0, totalTokens: 100, models: ["gpt-6-luna"], listPriceUsd: 0.01 }],
  } });
  tree = render(ui.Usage, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Analyze session")[0].props.onClick();
  tree = render(ui.Usage, hooks, props);
  const explanation = findAll(tree, (node) => node.type?.name === "SessionExplanation")[0];
  const explanationTree = render(explanation.type, createHooks(), explanation.props);
  assert.match(textOf(explanationTree), /Turn\s+1\s+used\s+100\s+reported tokens/);
  assert.match(textOf(explanationTree), /not model latency/);
  assert.equal(requests.length, 3);
});

test("selected day explains its own contributors and optional AI requires preview and approval", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { url: "/api/usage", compact: true, onOpenRun: (id) => opened.push(id) };
  const days = [
    { day: "2026-10-07", turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 0 },
    { day: "2026-10-08", turns: 1, tokenKnownTurns: 1, totalTokens: 300, pricedTurns: 0, sourcePartial: true },
  ];
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { window: { complete: true }, days, selectedDay: days[1].day,
    dayModels: [], daySources: [], sessions: [], selectedSession: null });
  let tree = render(ui.Usage, hooks, props);
  const chart = findAll(tree, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
  assert.match(findAll(chartTree, (node) => node.type === "button" &&
    node.props["aria-label"]?.includes("2026-10-08"))[0].props["aria-label"], /incomplete or unverified/);
  findAll(chartTree, (node) => node.type === "button" && node.props["aria-label"]?.includes("2026-10-07"))[0].props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], { window: { complete: true }, days, selectedDay: days[0].day,
    dayModels: [], daySources: [], sessions: [], selectedSession: null,
    dayExplanation: { day: days[0].day, totalTokens: 100, tokenKnownTurns: 1, turns: 1,
      windowComplete: true, comparison: null,
      topSessions: [{ sessionId: "session-a", sourceKind: "codex", dayTokens: 100,
        sharePercent: 100, tokenKnownTurns: 1, turns: 1 }],
      topTurns: [{ runId: "run-a", turnId: "turn-a", totalTokens: 100,
        sharePercent: 100, models: [] }] } });
  tree = render(ui.Usage, hooks, props);
  assert.equal(requests.length, 2, "selecting a day does not invoke an AI provider");
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Explain this day")[0].props.onClick();
  tree = render(ui.Usage, hooks, props);
  const component = findAll(tree, (node) => node.type?.name === "DayExplanation")[0];
  const explanation = render(component.type, createHooks(), component.props);
  assert.match(textOf(explanation), /100\s+reported tokens/);
  assert.match(textOf(explanation), /No reliable prior-day comparison/);
  const partialDay = render(component.type, createHooks(), { ...component.props,
    explanation: { ...component.props.explanation, sourcePartial: true } });
  assert.match(textOf(partialDay), /incomplete or unverified/);
  findAll(explanation, (node) => node.type === "button" && textOf(node) === "Open run")[0].props.onClick();
  assert.deepEqual(opened, ["run-a"]);

  const aiHooks = createEffectHooks();
  const aiProps = { root: "", day: days[0].day };
  render(ui.DayAiExplanation, aiHooks, aiProps);
  aiHooks.flushEffects();
  assert.equal(requests.length, 4);
  await resolveJson(requests[2], { setupToken: "local-token" });
  await resolveJson(requests[3], { providers: [{ provider: "anthropic", configured: true }] });
  let ai = render(ui.DayAiExplanation, aiHooks, aiProps);
  assert.equal(requests.length, 4, "opening the panel does not make a paid call");
  const previewing = findAll(ai, (node) => node.type === "button" && textOf(node) === "Preview AI context")[0].props.onClick();
  assert.equal(requests[4].url, "/api/usage/explain/preview");
  assert.equal(findAll(render(ui.DayAiExplanation, aiHooks, aiProps),
    (node) => node.type === "select")[0].props.disabled, true);
  await resolveJson(requests[4], { day: days[0].day, provider: "anthropic", model: "claude-haiku-4-5",
    destination: "https://api.anthropic.com", planFingerprint: "fingerprint", plannedCalls: 1,
    providerAvailable: true,
    upperStaticListPriceUsd: 0.01, messages: [{ role: "user", content: "aggregate metrics" }] });
  await previewing;
  ai = render(ui.DayAiExplanation, aiHooks, aiProps);
  const run = findAll(ai, (node) => node.type === "button" && textOf(node) === "Ask AI why")[0];
  assert.equal(run.props.disabled, true);
  findAll(ai, (node) => node.type === "checkbox" || node.type === "input" && node.props.type === "checkbox")[0].props.onChange({ target: { checked: true } });
  ai = render(ui.DayAiExplanation, aiHooks, aiProps);
  const asking = findAll(ai, (node) => node.type === "button" && textOf(node) === "Ask AI why")[0].props.onClick();
  assert.equal(requests[5].url, "/api/usage/explain/run");
  assert.equal(findAll(render(ui.DayAiExplanation, aiHooks, aiProps),
    (node) => node.type === "select")[0].props.disabled, true);
  assert.deepEqual(JSON.parse(requests[5].options.body), { day: days[0].day, provider: "anthropic",
    planFingerprint: "fingerprint", confirmExternalEgress: true });
  await resolveJson(requests[5], { status: "completed", answer: "Session 1 contributed most." });
  await asking;
  assert.match(textOf(render(ui.DayAiExplanation, aiHooks, aiProps)), /Session 1 contributed most/);
});

test("Inspect session pins the displayed UTC day before loading its detail", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage" };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], {
    window: { complete: true },
    days: [{ day: "2026-10-08", turns: 1, tokenKnownTurns: 1, totalTokens: 100 }],
    selectedDay: "2026-10-08", dayModels: [], daySources: [], sessions: [],
    selectedSession: null,
    dayExplanation: { day: "2026-10-08", totalTokens: 100, tokenKnownTurns: 1,
      turns: 1, windowComplete: true, comparison: null,
      topSessions: [{ sessionId: "session-a", sourceKind: "codex", dayTokens: 100,
        tokenKnownTurns: 1, turns: 1 }], topTurns: [] },
  });
  let tree = render(ui.Usage, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Explain this day")[0].props.onClick();
  tree = render(ui.Usage, hooks, props);
  findAll(tree, (node) => node.type?.name === "DayExplanation")[0]
    .props.onSelectSession({ sessionId: "session-a", sourceKind: "codex" });
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  assert.equal(requests[1].url,
    "/api/usage?day=2026-10-08&session_id=session-a&source_kind=codex");
});

test("usage labels identify source once and turns across runs", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage" };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], {
    window: { complete: true, turnsComplete: true },
    days: [{ day: "2026-10-08", turns: 2, tokenKnownTurns: 2, totalTokens: 200,
      pricedTurns: 0, listPriceUsd: null }],
    selectedDay: "2026-10-08", dayModels: [], daySources: [],
    sessions: [{ sessionId: "session-a", agentName: "codex", sourceKind: "codex",
      runIds: ["run-a", "run-b"], turns: 2, tokenKnownTurns: 2,
      totalTokens: 200, pricedTurns: 0, listPriceUsd: null }],
    selectedSession: { sessionId: "session-a", sourceKind: "codex", turnCount: 2,
      totalTokens: 200, pricedTurns: 0, listPriceUsd: null,
      turns: ["run-a", "run-b"].map((runId) => ({ runId, turnId: `${runId}-turn`,
        sequence: 19, totalTokens: 100, models: [], listPriceUsd: null })) },
  });
  const tree = render(ui.Usage, hooks, props);
  const labels = textOf(tree);
  assert.doesNotMatch(labels, /codex\s*·\s*codex/);
  assert.match(labels, /run-a.*Turn\s+20/);
  assert.match(labels, /run-b.*Turn\s+20/);
});

test("usage explains an ambiguous source-qualified session selection", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage" };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], {
    window: { complete: true }, selectedDay: "2026-10-08",
    days: [{ day: "2026-10-08", turns: 2, tokenKnownTurns: 2,
      totalTokens: 300, pricedTurns: 0, listPriceUsd: null }],
    dayModels: [], daySources: [], sessionsTruncated: false,
    sessions: ["codex", "claude-code"].map((sourceKind) => ({
      sourceKind, sessionId: "shared-session", agentName: sourceKind,
      runIds: [sourceKind], totalTokens: 150, tokenKnownTurns: 1, turns: 1,
      pricedTurns: 0, listPriceUsd: null,
    })),
    selectedSession: null, selectionReason: "ambiguous_source",
  });
  const tree = render(ui.Usage, hooks, props);
  assert.match(textOf(tree), /session ID appears in more than one agent source/);
  assert.equal(requests.length, 1);
});

test("usage ignores an older day response after a newer selection", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage" };
  const days = [
    { day: "2026-10-07", turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 0, listPriceUsd: null },
    { day: "2026-10-08", turns: 1, tokenKnownTurns: 1, totalTokens: 200, pricedTurns: 0, listPriceUsd: null },
  ];
  const snapshot = (selectedDay) => ({
    window: { complete: true }, days, selectedDay, dayModels: [], sessions: [],
    sessionsTruncated: false, selectedSession: null,
  });
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot("2026-10-08"));
  const selectDay = (value) => {
    const tree = render(ui.Usage, hooks, props);
    const chart = findAll(tree, (node) => node.type?.name === "UsageChart")[0];
    const chartTree = render(chart.type, createHooks(), chart.props);
    findAll(chartTree, (node) => node.type === "button" && node.props["aria-label"]?.includes(value))[0].props.onClick();
    render(ui.Usage, hooks, props);
    hooks.flushEffects();
  };
  selectDay("2026-10-07");
  selectDay("2026-10-08");
  await resolveJson(requests[2], snapshot("2026-10-08"));
  await resolveJson(requests[1], snapshot("2026-10-07"));
  const current = render(ui.Usage, hooks, props);
  assert.match(textOf(current), /2026-10-08\s+UTC/);
  assert.doesNotMatch(textOf(current), /Usage unavailable/);
});

test("session usage is shown only for a live store", async () => {
  const ui = await loadUiModule();
  globalThis.window = {
    location: { hash: "#tab=overview&section=performance", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const sample = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "sample" });
    const live = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" });
    assert.equal(findAll(sample, (node) => node.type?.name === "Usage").length, 0);
    assert.equal(findAll(live, (node) => node.type?.name === "Usage").length, 1);
    assert.equal(findAll(live, (node) => node.type === "details"
      && textOf(node).includes("Model-call diagnostics")).length, 1);
  } finally { delete globalThis.window; }
});

test("agent Overview starts with a guided home containing evidence and usage", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.meta.totalAgentRuns = 2;
  data.monitor = { state: "not_configured", active: null, candidate: null };
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    const homes = findAll(tree, (node) => node.type?.name === "GuidedHome");
    assert.equal(homes.length, 1);
    assert.equal(homes[0].props.meta.totalAgentRuns, 2);
    assert.equal(homes[0].props.source, "live");
    const home = render(homes[0].type, createHooks(), homes[0].props);
    assert.match(textOf(home), /Captured agent runs/);
    assert.match(textOf(home), /Usage over time/);
    assert.match(textOf(home), /Recorded findings and evidence gaps linked to affected runs/);
    assert.equal(findAll(home, (node) => node.type?.name === "Insights").length, 1);
    assert.equal(findAll(home, (node) => node.type?.name === "Usage").length, 1);
  } finally { delete globalThis.window; }
});

test("first-run home directs capture without offering empty analysis", async () => {
  const ui = await loadUiModule();
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "new" });
    const home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    const summary = render(home.type, createHooks(), home.props);
    assert.match(textOf(summary), /Connect an agent or model source/);
    assert.equal(findAll(summary, (node) => node.type?.name === "Insights").length, 0);
    assert.doesNotMatch(textOf(summary), /Run initial analysis|Token trend needs agent turns/);
  } finally { delete globalThis.window; }
});

test("Home analysis action names its navigation rather than implying the check ran", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.GuidedHome, createHooks(), {
    meta: { totalAgentRuns: 1, monitorEligibleCalls: 0 }, source: "new",
    configUrl: "/api/config", insightsUrl: "/api/insights", usageUrl: "/api/usage",
    onNavigate() {}, onReload() {},
  });
  const [action] = findAll(tree, (node) => node.type === "button"
    && node.props.className === "verdict-home-primary-action");
  assert.ok(action);
  assert.equal(textOf(action).trim(), "Go to local analysis");
});

test("Overview does not report placeholder zero counts while live evidence loads", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.GuidedHome, createHooks(), {
    meta: { totalAgentRuns: 0, totalTraces: 0, monitorEligibleCalls: 0 },
    source: "loading", configUrl: "/api/config", insightsUrl: "/api/insights",
  });
  assert.match(textOf(tree), /Loading captured activity/);
  assert.doesNotMatch(textOf(tree), /AGENT RUNS|MONITOR-ELIGIBLE MODEL CALLS/);
});

test("monitoring destinations wait for current evidence instead of briefly claiming zero calls", async () => {
  const ui = await loadUiModule();
  try {
    for (const section of ["plan", "status", "segments", "history"]) {
      globalThis.window = { location: { hash: `#tab=monitor&section=${section}`, pathname: "/dashboard", search: "" },
        history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
      const props = { data: bundle("judge-a"), source: "loading" };
      let tree = render(ui.Dashboard, createHooks(), props);
      assert.match(textOf(tree), /Checking captured evidence and monitoring setup/);
      assert.match(textOf(tree), /Captured evidence not loaded yet/);
      assert.doesNotMatch(textOf(tree), /0 recorded agent runs · 0 model calls/);
      assert.equal(findAll(tree, (node) => ["Monitor", "IssueInbox", "Registry", "ControlCenter"].includes(node.type?.name)).length, 0);
      tree = render(ui.Dashboard, createHooks(), { ...props, loadError: "unavailable", reloading: false });
      assert.match(textOf(tree), /Could not load this workspace's evidence/);
      assert.match(textOf(tree), /Evidence unavailable/);
      assert.match(textOf(tree), /Retry loading evidence/);
    }
  } finally { delete globalThis.window; }
});

test("guided home waits for current readiness before claiming monitoring status", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.meta.totalAgentRuns = 2;
  data.monitor = { state: "not_configured", active: null, candidate: null };
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    let tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    let home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    assert.match(textOf(render(home.type, createHooks(), home.props)), /Checking captured evidence and setup/);

    data.monitor = { active: null, candidate: { state: "candidate",
      snapshot: { comparison: { status: "no_alert", metrics: [] } } } };
    tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    assert.doesNotMatch(textOf(render(home.type, createHooks(), home.props)), /Latest comparison found no alert/);

    data.monitor = { active: { state: "active",
      snapshot: { comparison: { status: "alert", metrics: [{ alert: true }] } } }, candidate: null };
    tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    assert.doesNotMatch(textOf(render(home.type, createHooks(), home.props)), /A monitored change needs review/);
  } finally { delete globalThis.window; }
});

test("refreshing the dashboard also refreshes the Overview worker status", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", source: "live",
    meta: { totalAgentRuns: 2, totalTraces: 0, monitorEligibleCalls: 0 }, monitor: null };
  render(ui.GuidedHome, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
    worker: { status: "stale" } });
  assert.match(textOf(render(ui.GuidedHome, hooks, props)), /Schedule saved; worker not verified/);
  const refreshed = { ...props, meta: { ...props.meta } };
  render(ui.GuidedHome, hooks, refreshed);
  hooks.flushEffects();
  assert.equal(requests.length, 2);
  await resolveJson(requests[1], { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
    worker: { status: "recent" } });
  assert.match(textOf(render(ui.GuidedHome, hooks, refreshed)), /Recurring worker seen recently/);
});

test("empty Home does not imply recurring checks have run", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", source: "live",
    meta: { totalAgentRuns: 0, totalTraces: 0, monitorEligibleCalls: 0 }, monitor: null };
  render(ui.GuidedHome, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  const summary = textOf(render(ui.GuidedHome, hooks, props));
  assert.match(summary, /Connect an agent or model source/);
  assert.match(summary, /No recurring check configured/);
  assert.doesNotMatch(summary, /Open recurring issues|since monitoring began/);
});

test("Home issue metric leads with saved findings when a schedule has no new notices", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", source: "live", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 4, totalTraces: 0, monitorEligibleCalls: 0, evidenceRevision: 1 }, monitor: null };
  let tree = render(ui.GuidedHome, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { openIssues: 0,
    documents: [{ kind: "schedule", documentId: "daily", state: "active" }], worker: { status: "stale" } });
  tree = render(ui.GuidedHome, hooks, props);
  findAll(tree, (node) => node.type?.name === "Insights")[0].props.onData({
    analysisState: { status: "completed", verifiedRevision: 1 },
    scope: { availableRuns: 4, traces: { available: 0 } },
    findings: [
      { code: "test_failure", severity: "error", runIds: ["run-1"] },
      { code: "tool_failure", severity: "warning", runIds: ["run-2"] },
    ],
  }, null);
  tree = render(ui.GuidedHome, hooks, props);
  const metrics = findAll(tree, (node) => node.props?.className === "verdict-home-metric");
  assert.match(textOf(metrics[2]), /Recorded issue types 2 Current analyzed scope · 0 recurring notices/);
  assert.doesNotMatch(textOf(metrics[2]), /Open recurring issues 0/);
  assert.match(textOf(tree), /Schedule saved; worker not verified/);
  const refreshed = { ...props, meta: { ...props.meta } };
  render(ui.GuidedHome, hooks, refreshed);
  hooks.flushEffects();
  await resolveJson(requests[1], { openIssues: 1,
    documents: [{ kind: "schedule", documentId: "daily", state: "active" }], worker: { status: "recent" } });
  tree = render(ui.GuidedHome, hooks, refreshed);
  const updatedMetrics = findAll(tree, (node) => node.props?.className === "verdict-home-metric");
  assert.match(textOf(updatedMetrics[2]),
    /Open recurring issues 1 Recorded failed events or terminal run statuses/);
});

test("Home formats capture timestamps for people instead of exposing raw ISO values", async () => {
  const ui = await loadUiModule();
  const stamp = "2026-10-18T04:19:50.368014+00:00";
  const tree = render(ui.GuidedHome, createHooks(), {
    configUrl: "/api/config", source: "sample",
    meta: { totalAgentRuns: 1, lastAgentCaptureAt: stamp }, monitor: null,
  });
  const summary = textOf(tree);
  assert.match(summary, /Latest agent run started:/);
  assert.doesNotMatch(summary, /2026-10-18T04:19:50/);
  assert.match(summary, /UTC/);
});

test("Home presents evidence before operational status and timestamps", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.GuidedHome, createHooks(), {
    configUrl: "/api/config", insightsUrl: "/api/insights", usageUrl: "/api/usage",
    source: "live", meta: { totalAgentRuns: 1, monitorEligibleCalls: 1,
      lastAgentCaptureAt: "2026-10-18T04:19:50Z" },
  });
  const summary = textOf(tree);
  assert.ok(summary.indexOf("Usage over time") < summary.indexOf("Latest agent run started:"));
  assert.ok(summary.indexOf("What to look at") < summary.indexOf("Latest agent run started:"));
});

test("Home names issue-only evidence without promising a token chart", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights", usageUrl: "/api/usage",
    source: "live", meta: { totalAgentRuns: 1, monitorEligibleCalls: 0 } };
  let tree = render(ui.GuidedHome, hooks, props);
  findAll(tree, (node) => node.type?.name === "Usage")[0].props.onData("/api/usage", {
    days: [{ day: "2026-10-19", tokenKnownTurns: 0, recordedIssueEvents: 1 }],
  });
  tree = render(ui.GuidedHome, hooks, props);
  assert.match(textOf(tree), /Recorded issues by day/);
  assert.match(textOf(tree), /Choose a day to inspect recorded issue events/);
  assert.doesNotMatch(textOf(tree), /Choose a day to inspect the captured token evidence/);
});

test("AI agent-run setup draft returns to guided checks instead of statistical drift", async () => {
  const ui = await loadUiModule();
  const opened = [];
  const tree = render(ui.Monitor, createHooks(), { configUrl: "/api/config", view: "history",
    traceCount: 1, agentRunCount: 1, onPlan: () => opened.push("plan"),
    onSetup: () => opened.push("capture") });
  const assist = findAll(tree, (node) => node.type?.name === "EvidenceExplain")[0];
  assert.ok(assist);
  assist.props.onApplyDraft({ check: "agent_runs", grouping: "none", intervalHours: 24 });
  assert.deepEqual(opened, ["plan"]);
  assist.props.onApplyDraft({ check: "capture_first", grouping: "none", intervalHours: 24 });
  assert.deepEqual(opened, ["plan", "capture"]);
});

test("AI cohort draft fills only local comparison controls until preview", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const props = { configUrl: "/api/config", view: "history", traceCount: 60 };
  let tree = render(ui.Monitor, hooks, props);
  const assist = findAll(tree, (node) => node.type?.name === "EvidenceExplain"
    && node.props.draftKind === "cohort")[0];
  assert.ok(assist);
  assist.props.onApplyDraft({ grouping: "none", referenceShare: 50,
    minimumReference: 30, minimumCurrent: 30, prospectiveTarget: 33,
    why: "Review the historical split." });
  tree = render(ui.Monitor, hooks, props);
  const inputs = findAll(tree, (node) => node.type === "input" && node.props.type === "number");
  assert.ok(inputs.some((node) => node.props.value === 0.5));
  assert.ok(inputs.some((node) => node.props.value === 30));
  assert.ok(inputs.some((node) => node.props.value === 33));
  assert.doesNotMatch(textOf(tree), /Review before activating/);
});

test("unrun analysis uses current captured count, not the empty saved snapshot", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/insights", mode: "home", currentMeta: { totalAgentRuns: 1 } };
  render(ui.Insights, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { analysisState: { status: "never_run" },
    scope: { complete: true, analyzedRuns: 0, availableRuns: 0 },
    dataHealth: { counts: { runs: 0, turns: 0 }, traceEvidence: {},
      eventTypes: {}, eventStatuses: {} },
    performance: {}, reliability: {}, behavior: {}, findings: [] });
  const tree = render(ui.Insights, hooks, props);
  const summary = textOf(tree);
  assert.match(summary, /1 captured run/);
  assert.doesNotMatch(summary, /0 captured runs|Capture or import evidence, then run local analysis/);
  assert.doesNotMatch(summary, /Activity and evidence breakdown/);
  assert.equal(findAll(tree, (node) => node.type === "button"
    && /Run (initial|local) analysis/.test(textOf(node))).length, 1);
});

test("Reliability shows sparse tool-result records beside a zero failure count", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const snapshot = insightsSnapshot("completed");
  snapshot.scope = { ...snapshot.scope, availableRuns: 1, analyzedRuns: 1 };
  snapshot.dataHealth.counts = { runs: 1, turns: 1, events: 11 };
  snapshot.reliability = { ...snapshot.reliability, toolErrors: 0, toolOutcomesReported: 1 };
  snapshot.performance.toolCalls = 10;
  snapshot.sourceActivity = [{ source: "synthetic", runs: 1, childRuns: 0,
    turns: 1, finalResponses: 0, truncatedResponses: 0, toolCalls: 10,
    toolErrors: 0, toolOutcomesReported: 1, commandFailures: 0,
    commandOutcomesReported: 0, testFailures: 0, testOutcomesReported: 0,
    pricedTurns: 0, tokenUsageTurns: 0, totalTokenTurns: 0, runOutcomes: {} }];
  render(ui.Insights, hooks, { url: "/api/insights", mode: "reliability" });
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  const reliability = findAll(render(ui.Insights, hooks,
    { url: "/api/insights", mode: "reliability" }),
  (node) => node.type?.name === "ProductView")[0].props.rows.flat().join(" ");
  assert.match(reliability, /10 tool call records/);
  assert.match(reliability, /1 reported tool result/);
  assert.doesNotMatch(reliability, /1 reported tool results/);
  assert.match(reliability, /Tool errors among reported results/);

  const breakdown = textOf(render(ui.Insights, hooks,
    { url: "/api/insights", mode: "findings" }));
  assert.match(breakdown, /Tool calls \/ reported results/);
  assert.match(breakdown, /10\s+\/\s+1/);
  assert.match(breakdown, /do not establish one-to-one pairing/);

  const orphanHooks = createEffectHooks();
  const orphanRequests = deferredFetches();
  snapshot.performance.toolCalls = 0;
  snapshot.reliability.toolOutcomesReported = 2;
  render(ui.Insights, orphanHooks, { url: "/api/insights", mode: "reliability" });
  orphanHooks.flushEffects();
  await resolveJson(orphanRequests[0], snapshot);
  const orphanRows = findAll(render(ui.Insights, orphanHooks,
    { url: "/api/insights", mode: "reliability" }),
  (node) => node.type?.name === "ProductView")[0].props.rows.flat().join(" ");
  assert.match(orphanRows, /0 tool call records · 2 reported tool results/);
});

test("Reliability labels missing provider errors without inferring success", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const snapshot = insightsSnapshot("completed");
  snapshot.reliability.traceOutcomes = { error_recorded: 1, ended_no_error_recorded: 1, unfinished: 1 };
  render(ui.Insights, hooks, { url: "/api/insights", mode: "reliability" });
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  const rows = findAll(render(ui.Insights, hooks,
    { url: "/api/insights", mode: "reliability" }),
  (node) => node.type?.name === "ProductView")[0].props.rows;
  const outcomes = rows.find(([label]) => label === "LLM trace evidence")[1];
  assert.match(outcomes, /Provider error recorded: 1/);
  assert.match(outcomes, /Ended without recorded provider error: 1/);
  assert.match(outcomes, /Unfinished: 1/);
  assert.doesNotMatch(outcomes, /succeeded|failed|no_error_recorded/);
});

test("detail views disclose incomplete Agent Run and Trace scans", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const snapshot = insightsSnapshot("completed");
  snapshot.scope = { availableRuns: 10, analyzedRuns: 8, complete: false,
    traces: { available: 20, analyzed: 15, complete: false } };
  const props = { url: "/api/insights", mode: "reliability" };
  render(ui.Insights, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  const product = findAll(render(ui.Insights, hooks, props),
    (node) => node.type?.name === "ProductView")[0];
  const body = textOf(product.type(product.props));
  assert.match(body, /Agent Run scan is partial:\s+8\s+of\s+10\s+runs/);
  assert.match(body, /Trace scan is partial:\s+15\s+of\s+20\s+traces/);
  assert.doesNotMatch(body, /dataset-wide bounded scan/);
});

test("Insights does not call a historical-prefix analysis a clean complete run", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const snapshot = insightsSnapshot("completed");
  snapshot.scope = { ...snapshot.scope, availableRuns: 1, analyzedRuns: 1,
    partialRuns: 1, complete: false };
  snapshot.dataHealth.counts = { runs: 1, turns: 1000, events: 0 };
  const props = { url: "/api/insights", mode: "home" };
  render(ui.Insights, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  const body = textOf(render(ui.Insights, hooks, props));
  assert.match(body, /recorded prefix/);
  assert.match(body, /whole-run outcomes are excluded/);
  assert.doesNotMatch(body, /No deterministic issues found in the analyzed snapshot/);
});

test("daily token bars use a readable color and horizontal month-day ticks", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const days = Array.from({ length: 30 }, (_, index) => ({
    day: new Date(Date.UTC(2026, 8, 14 + index)).toISOString().slice(0, 10),
    turns: 1, tokenKnownTurns: 1, totalTokens: 100 + index,
    pricedTurns: 0, listPriceUsd: null,
  }));
  days[0] = { ...days[0], turns: 0, tokenKnownTurns: 0, totalTokens: 0 };
  days[1] = { ...days[1], turns: 1, tokenKnownTurns: 0, totalTokens: null };
  days[2] = { ...days[2], turns: 1, tokenKnownTurns: 1, totalTokens: 0 };
  render(ui.Usage, hooks, { url: "/api/usage", compact: true });
  hooks.flushEffects();
  await resolveJson(requests[0], { window: { complete: true, turnsComplete: true }, days,
    selectedDay: days.at(-1).day, dayModels: [], daySources: [], sessions: [],
    sessionsTruncated: false, selectedSession: null });
  const usage = render(ui.Usage, hooks, { url: "/api/usage", compact: true });
  const chart = findAll(usage, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
  assert.match(textOf(chartTree), /Sep 14/);
  assert.match(textOf(chartTree), /Oct 13/);
  const ticks = findAll(chartTree, (node) => node.props?.style?.transform?.includes("rotate"));
  assert.equal(ticks.length, 0);
  const bars = findAll(chartTree, (node) => node.type === "button" && node.props["aria-label"]?.includes("reported tokens"));
  assert.equal(bars.length, 28);
  const empty = findAll(chartTree, (node) => node.type === "span" && node.props.role === "img"
    && node.props["aria-label"]?.startsWith(days[0].day))[0];
  assert.match(empty.props["aria-label"], /no captured turns/);
  assert.equal(findAll(chartTree, (node) => node.type === "button"
    && node.props["aria-label"]?.startsWith(days[0].day)).length, 0);
  const unknown = findAll(chartTree, (node) => node.type === "button"
    && node.props["aria-label"]?.startsWith(days[1].day))[0];
  assert.match(unknown.props["aria-label"], /token totals unavailable; 0\/1 turns/);
  const measuredZero = findAll(chartTree, (node) => node.type === "button"
    && node.props["aria-label"]?.startsWith(days[2].day))[0];
  assert.match(measuredZero.props["aria-label"], /0 reported tokens; 1\/1 turns/);
  const fill = findAll(bars[0], (node) => node.type === "span" && node.props.style?.background)[0];
  assert.notEqual(fill.props.style.background, "#94a39d");
});

test("compact token chart reveals sessions only after selecting a day", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage", compact: true };
  const snapshot = (selectedDay) => ({
    window: { complete: true, turnsComplete: true },
    days: [
      { day: "2026-10-06", turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 0, listPriceUsd: null },
      { day: "2026-10-07", turns: 1, tokenKnownTurns: 1, totalTokens: 200, pricedTurns: 0, listPriceUsd: null },
    ],
    selectedDay, dayModels: [], daySources: [], selectedSession: null,
    sessions: [{ sessionId: "session-a", sourceKind: "codex", agentName: "codex", runIds: ["run-a"],
      turns: 1, tokenKnownTurns: 1, totalTokens: 100, pricedTurns: 0, listPriceUsd: null }],
    sessionsTruncated: false,
  });
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot("2026-10-07"));
  let tree = render(ui.Usage, hooks, props);
  assert.ok(findAll(tree, (node) => node.type?.name === "UsageChart").length);
  assert.doesNotMatch(textOf(tree), /Sessions active on/);
  const chart = findAll(tree, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
  findAll(chartTree, (node) => node.type === "button" && node.props["aria-label"]?.includes("2026-10-06"))[0].props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], snapshot("2026-10-06"));
  tree = render(ui.Usage, hooks, props);
  assert.match(textOf(tree), /Sessions active on\s+2026-10-06\s+UTC/);
  assert.match(textOf(tree), /session-a|codex/);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("run"))[0].props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[2], { ...snapshot("2026-10-06"), selectedSession: {
    sessionId: "session-a", sourceKind: "codex", turnCount: 6, totalTokens: 600,
    pricedTurns: 0, listPriceUsd: null, turns: Array.from({ length: 6 }, (_, sequence) => ({
      sequence, runId: "run-a", turnId: `turn-${sequence}`, totalTokens: 100,
      models: [], listPriceUsd: null,
    })),
  } });
  tree = render(ui.Usage, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "button" && textOf(node) === "Open run").length, 6);
  assert.match(textOf(tree), /Show\s+1\s+more displayed turns/);
  assert.equal(findAll(tree, (node) => node.type?.name === "SessionReport").length, 0);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Open session report")[0].props.onClick();
  tree = render(ui.Usage, hooks, props);
  assert.equal(findAll(tree, (node) => node.type?.name === "SessionReport").length, 1);
});

test("missing token totals do not draw zero bars and still allow session inspection", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage", compact: true };
  const snapshot = { window: { complete: false, turnsComplete: true },
    days: [{ day: "2026-10-07", turns: 2, tokenKnownTurns: 0, totalTokens: null,
      pricedTurns: 0, listPriceUsd: null }], selectedDay: "2026-10-07",
    dayModels: [], daySources: [], sessions: [{ sessionId: "session-unknown", sourceKind: "new-agent",
      agentName: "new-agent", runIds: ["run-a"], turns: 2, tokenKnownTurns: 0,
      totalTokens: null, pricedTurns: 0, listPriceUsd: null }], sessionsTruncated: false,
    selectedSession: null };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  let tree = render(ui.Usage, hooks, props);
  assert.match(textOf(tree), /Session-level token attribution is unavailable/i);
  assert.match(textOf(tree), /bounded and partial/i);
  assert.equal(findAll(tree, (node) => node.type?.name === "UsageChart").length, 0);
  assert.equal(findAll(tree, (node) => node.type === "button" && textOf(node).includes("Est. list price")).length, 0);
  const inspect = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Inspect sessions with missing totals"))[0];
  inspect.props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], snapshot);
  tree = render(ui.Usage, hooks, props);
  assert.match(textOf(tree), /Sessions active on\s+2026-10-07\s+UTC/);
  assert.match(textOf(tree), /new-agent/);
});

test("Home names local source coverage rather than model attribution when only source is partial", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const report = { window: { complete: false, turnsComplete: true,
    modelEvidenceComplete: true },
    days: [{ day: "2026-10-13", turns: 1, tokenKnownTurns: 1,
      totalTokens: 900, pricedTurns: 0, listPriceUsd: null,
      sourcePartial: true, recordedIssueEvents: 1 }],
    selectedDay: "2026-10-13", selectedDayIssues: { complete: false,
      count: null, observedCount: 1, sourcePartial: true, events: [],
      eventsTruncated: false }, dayModels: [], daySources: [], sessions: [],
    sessionsTruncated: false, selectedSession: null };
  render(ui.Usage, hooks, { url: "/api/usage", compact: true, home: true });
  hooks.flushEffects();
  await resolveJson(requests[0], report);
  const tree = render(ui.Usage, hooks, { url: "/api/usage", compact: true, home: true });
  assert.match(textOf(tree), /Local turn history is incomplete or unverified/i);
  assert.doesNotMatch(textOf(tree), /Model attribution is incomplete for older turns/i);
});

test("Home links an issue day without drawing a zero-token chart when totals are missing", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { url: "/api/usage", compact: true, home: true,
    onOpenRun: (runId, eventId) => opened.push([runId, eventId]) };
  const snapshot = { window: { complete: true, turnsComplete: true },
    days: [{ day: "2026-10-19", turns: 2, tokenKnownTurns: 0, totalTokens: null,
      pricedTurns: 0, listPriceUsd: null, recordedIssueEvents: 1 }],
    selectedDay: "2026-10-19", selectedDayIssues: { complete: true, count: 1,
      events: [{ runId: "failed-run", eventId: "failed-event", code: "test_failed" }],
      eventsTruncated: false }, dayModels: [], daySources: [], sessions: [],
    sessionsTruncated: false, selectedSession: null };
  render(ui.Usage, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  let tree = render(ui.Usage, hooks, props);
  assert.equal(findAll(tree, (node) => node.type?.name === "UsageChart").length, 0);
  assert.doesNotMatch(textOf(tree), /Peak 0/);
  const inspect = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Inspect recorded issue events"))[0];
  assert.ok(inspect);
  assert.match(textOf(inspect), /2026-10-19/);
  inspect.props.onClick();
  render(ui.Usage, hooks, props); hooks.flushEffects();
  await resolveJson(requests[1], snapshot);
  tree = render(ui.Usage, hooks, props);
  const summary = findAll(tree, (node) => node.type?.name === "DayIssueSummary")[0];
  assert.ok(summary);
  const summaryTree = render(summary.type, createHooks(), summary.props);
  assert.match(textOf(summaryTree), /Recorded issues on\s+2026-10-19\s+UTC/);
  findAll(summaryTree, (node) => node.type === "button"
    && /Inspect\s+test failed/.test(textOf(node)))[0].props.onClick();
  assert.deepEqual(opened, [["failed-run", "failed-event"]]);

  const fullHooks = createEffectHooks();
  const fullRequests = deferredFetches();
  const fullProps = { url: "/api/usage", compact: false };
  render(ui.Usage, fullHooks, fullProps); fullHooks.flushEffects();
  await resolveJson(fullRequests[0], snapshot);
  const full = render(ui.Usage, fullHooks, fullProps);
  assert.equal(findAll(full, (node) => node.type?.name === "UsageChart").length, 1);
  assert.match(textOf(full), /Recorded issue events by day/);
  assert.match(textOf(full), /Choose an issue date to inspect its recorded events/);
  assert.doesNotMatch(textOf(full), /Choose a day to explain its token use/);
  assert.equal(findAll(full, (node) => node.type === "button" && textOf(node) === "Est. list price").length, 0);
  const chart = findAll(full, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
  assert.equal(findAll(chartTree, (node) => node.props?.role === "group")[0].props["aria-label"],
    "Recorded issue events by UTC day");
  assert.doesNotMatch(textOf(full), /Peak 0/);
});

test("Home does not show a previous day's issue while another day loads", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage", compact: true, home: true };
  const oldDay = { window: { complete: true, turnsComplete: true },
    days: [
      { day: "2026-10-18", turns: 1, tokenKnownTurns: 0, recordedIssueEvents: 0 },
      { day: "2026-10-19", turns: 0, tokenKnownTurns: 0, recordedIssueEvents: 1 },
    ],
    selectedDay: "2026-10-18", selectedDayIssues: { complete: true, count: 0, events: [] },
    dayModels: [], daySources: [], sessions: [], selectedSession: null };
  render(ui.Usage, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], oldDay);
  let tree = render(ui.Usage, hooks, props);
  findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Inspect recorded issue events"))[0].props.onClick();
  tree = render(ui.Usage, hooks, props);
  assert.equal(findAll(tree, (node) => node.type?.name === "DayIssueSummary").length, 0);
  assert.doesNotMatch(textOf(tree), /Recorded issues on 2026-10-18 UTC/);
});

test("provider-call token trend remains visible when agent-turn totals are missing", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { url: "/api/usage", compact: true, onOpenCalls: (date) => { opened.push(date); },
    modelCallReport: { period: { days: 7 }, timeline: { rows: [
      { date: "2026-10-06", calls: 3, tokenKnownCalls: 2, totalTokens: 120 },
      { date: "2026-10-07", calls: 1, tokenKnownCalls: 1, totalTokens: 60 },
    ] } } };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { window: { complete: true }, days: [],
    selectedDay: null, dayModels: [], daySources: [], sessions: [] });
  const tree = render(ui.Usage, hooks, props);
  assert.doesNotMatch(textOf(tree), /LAST 30 UTC DAYS/);
  const trend = findAll(tree, (node) => node.type?.name === "ModelCallTrend")[0];
  assert.ok(trend);
  const chartHooks = createHooks();
  let chart = render(trend.type, chartHooks, trend.props);
  assert.match(textOf(chart), /last 7 UTC days/i);
  assert.ok(findAll(chart, (node) => node.type === "button"
    && node.props["aria-label"]?.includes("2/3 calls with totals")).length);
  assert.match(textOf(chart), /2026-10-07\s+UTC/);
  findAll(chart, (node) => node.type === "button" && textOf(node).includes("Inspect model calls"))[0].props.onClick();
  assert.deepEqual(opened, ["2026-10-07"]);
  findAll(chart, (node) => node.type === "button" && node.props["aria-label"]?.startsWith("2026-10-06"))[0]
    .props.onClick();
  chart = render(trend.type, chartHooks, trend.props);
  findAll(chart, (node) => node.type === "button" && textOf(node).includes("Inspect model calls"))[0].props.onClick();
  assert.deepEqual(opened, ["2026-10-07", "2026-10-06"]);
});

test("model-call tokens do not relabel an Agent issue-only timeline as usage", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage", compact: false,
    modelCallReport: { timeline: { rows: [
      { date: "2026-10-19", calls: 1, tokenKnownCalls: 1, totalTokens: 100 },
    ] } } };
  render(ui.Usage, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { window: { complete: true }, days: [
    { day: "2026-10-19", turns: 1, tokenKnownTurns: 0, recordedIssueEvents: 1 },
  ], selectedDay: "2026-10-19", selectedDayIssues: { complete: true, count: 1, events: [] },
  dayModels: [], daySources: [], sessions: [], selectedSession: null });
  const tree = render(ui.Usage, hooks, props);
  assert.equal(findAll(tree, (node) => node.type?.name === "ModelCallTrend").length, 1);
  assert.match(textOf(tree), /Recorded issue events by day/);
  const chart = findAll(tree, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
  assert.equal(findAll(chartTree, (node) => node.props?.role === "group")[0].props["aria-label"],
    "Recorded issue events by UTC day");
});

test("Home shows available model-call chart before missing session attribution", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/usage", compact: true, home: true,
    modelCallReport: { period: { days: 30 }, timeline: { rows: [
      { date: "2026-10-18", calls: 2, tokenKnownCalls: 1, totalTokens: 15 },
    ] } } };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { window: { complete: true }, days: [
    { day: "2026-10-18", turns: 2, tokenKnownTurns: 0, totalTokens: null },
  ], selectedDay: "2026-10-18", dayModels: [], daySources: [], sessions: [] });
  const tree = render(ui.Usage, hooks, props);
  const sections = findAll(tree, (node) => node.type?.name === "ModelCallTrend"
    || node.type === "p" && textOf(node).includes("Session-level token attribution"));
  assert.equal(sections[0].type?.name, "ModelCallTrend");
  assert.match(textOf(sections[1]), /Session-level token attribution/);
});

test("guided setup describes both turn and model-call usage paths", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = {
    configUrl: "/api/config", section: "schedule", embedded: true,
    meta: { totalAgentRuns: 1, monitorEligibleCalls: 1 },
  };
  render(ui.ControlCenter, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { documents: [], worker: { status: "unverified" },
    notifications: [], deliveryAttempts: [], reviewQueue: [] });
  const summary = textOf(render(ui.ControlCenter, hooks, props));
  assert.match(summary, /reported turn or model-call totals/);
  assert.match(summary, /matching sessions or calls/);
  assert.doesNotMatch(summary, /follow a day into its sessions and turns/);
});

test("model-call trend names the report period and bounded display", async () => {
  const ui = await loadUiModule();
  for (const [days, label] of [[90, "last 90 UTC days"], [0, "all captured dates"]]) {
    const hooks = createEffectHooks();
    const requests = deferredFetches();
    const props = { url: "/api/usage", compact: true, modelCallReport: {
      period: { days }, timeline: { availableDates: 40, rows: [
        { date: "2026-10-07", calls: 1, tokenKnownCalls: 1, totalTokens: 60 },
      ] } } };
    render(ui.Usage, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], { window: { complete: true }, days: [], selectedDay: null,
      dayModels: [], daySources: [], sessions: [] });
    const tree = render(ui.Usage, hooks, props);
    const trend = findAll(tree, (node) => node.type?.name === "ModelCallTrend")[0];
  const chart = render(trend.type, createHooks(), trend.props);
  assert.match(textOf(chart), new RegExp(label, "i"));
  assert.match(textOf(chart), /newest\s+1\s+of\s+40\s+active dates/i);
  const bar = findAll(chart, (node) => node.type === "button" && node.props["aria-label"]?.startsWith("2026-10-07"))[0];
  assert.equal(bar.props.style.width, 40);
  assert.equal(findAll(chart, (node) => node.type === "span" && textOf(node) === "2026-10-07").length, 1);
  }
});

test("Insights reloads the normalized snapshot after an error retry", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  let dashboardRefreshes = 0;
  const props = { url: "/api/insights?tenant=local", onAnalysisComplete: () => { dashboardRefreshes += 1; } };

  render(ui.Insights, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], insightsSnapshot("error"));

  let tree = render(ui.Insights, hooks, props);
  hooks.flushEffects();
  const retry = findAll(
    tree,
    (node) => node.type === "button" && textOf(node).trim() === "Retry",
  )[0];
  const pending = retry.props.onClick();
  await resolveJson(requests[1], { setupToken: "setup-token" });
  assert.equal(requests[2].options.method, "POST");
  await resolveJson(requests[2], {
    schema: "agent-insights-v2",
    error: { code: "analysis_failed", causeType: "OperationalError" },
    analysisState: { status: "error" },
  });

  assert.equal(requests.length, 4);
  assert.match(requests[3].url, /\/api\/insights\?tenant=local$/);
  assert.equal(requests[3].options.method, undefined);
  await resolveJson(requests[3], insightsSnapshot("error"));
  await pending;
  assert.equal(dashboardRefreshes, 1, "the dashboard coverage must refresh with the insights snapshot");

  tree = render(ui.Insights, hooks, props);
  assert.match(textOf(tree), /The latest analysis failed/);
  const agentRuns = findAll(
    tree,
    (node) => node.type?.name === "Metric" && node.props?.label === "Agent runs",
  )[0];
  assert.equal(agentRuns.props.value, 0);
});

test("Overview leads with actionable issues and keeps evidence limits and technical detail available", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { url: "/api/insights", mode: "home", onOpenRuns: (finding) => opened.push(finding.code) };
  const snapshot = insightsSnapshot("completed");
  snapshot.scope = { ...snapshot.scope, availableRuns: 10, analyzedRuns: 8, complete: false };
  snapshot.dataHealth.counts.runs = 10;
  snapshot.findings = [
    { code: "run_status_unknown", severity: "info", runs: 7, message: "Status unavailable." },
    { code: "possible_tool_loop", severity: "warning", runs: 3, message: "Repeated identical calls." },
    { code: "tool_error", severity: "error", runs: 2, message: "Tool result reported failure." },
  ];
  render(ui.Insights, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  const tree = render(ui.Insights, hooks, props);
  const chart = findAll(tree, (node) => node.props?.role === "group"
    && node.props?.["aria-label"] === "Recorded issues by affected runs")[0];
  assert.ok(chart, "the first-screen issue counts should be a clickable chart");
  const issueButtons = findAll(chart, (node) => node.type === "button" && node.props.onClick);
  assert.equal(issueButtons.length, 2);
  assert.match(textOf(issueButtons[0]), /Tool failures recorded.*2\s+run/s);
  assert.match(textOf(issueButtons[1]), /Repeated identical tool calls.*3\s+run/s);
  assert.match(textOf(tree), /8 of 10 runs in this analysis snapshot/i);
  assert.match(textOf(tree), /This view is partial/i);
  assert.equal(findAll(tree, (node) => node.type === "details").length, 2);
  assert.equal(findAll(tree, (node) => node.type === "details"
    && textOf(node).includes("Evidence gaps"))[0].props.open, false);
  issueButtons[0].props.onClick();
  const gapButton = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Run outcome unavailable"))[0];
  gapButton.props.onClick();
  assert.deepEqual(opened, ["tool_error", "run_status_unknown"]);
});

test("Home shows evidence gaps immediately when no recorded issue is found", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { url: "/api/insights", mode: "home", onOpenRuns: (finding) => opened.push(finding.code) };
  const snapshot = insightsSnapshot("completed");
  snapshot.findings = [{ code: "event_capture_partial", severity: "info", runs: 1,
    message: "Later events were not captured." }];
  render(ui.Insights, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], snapshot);
  const tree = render(ui.Insights, hooks, props);
  assert.match(textOf(tree), /No recorded issues in captured evidence.*review evidence coverage below/i);
  const gap = findAll(tree, (node) => node.type === "details"
    && textOf(node).includes("Evidence gaps"))[0];
  assert.equal(gap.props.open, true);
  findAll(gap, (node) => node.type === "button" && textOf(node).includes("Event capture reached its limit"))[0].props.onClick();
  assert.deepEqual(opened, ["event_capture_partial"]);
});

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

test("the full-page Operations control navigates the current tab only at the exact path", async () => {
  const ui = await loadUiModule();
  const assigned = [];
  globalThis.window = {
    location: {
      hash: "#tab=overview&section=summary", pathname: "/dashboard",
      assign(destination) { assigned.push(destination); },
    },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const withoutPage = render(ui.Dashboard, createHooks(), {
      data: bundle("judge-a"), operationsPageUrl: null,
    });
    const withPage = render(ui.Dashboard, createHooks(), {
      data: bundle("judge-a"), operationsPageUrl: "/operations",
    });

    assert.equal(findAll(withoutPage,
      (node) => node.type === "button" && textOf(node).trim() === "Operations").length, 0);
    const controls = findAll(withPage,
      (node) => node.type === "button" && textOf(node).trim() === "Operations");
    assert.equal(controls.length, 1);
    assert.equal(findAll(withPage,
      (node) => node.type === "a" && node.props?.href === "/operations").length, 0);

    controls[0].props.onClick();
    assert.deepEqual(assigned, ["/operations"]);
  } finally { delete globalThis.window; }
});

test("public query links select exact traces, runs, and events at the browser sink", async () => {
  const ui = await loadUiModule();
  const renderSelection = (search) => {
    globalThis.window = {
      location: { hash: "", pathname: "/dashboard", search },
      history: { pushState() {}, replaceState() {} },
      addEventListener() {}, removeEventListener() {},
    };
    return render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live",
      loadedTraceFilter: { traceDate: null, traceJudgeStatus: "all",
        traceId: search === "?view=traces&trace_id=trace-1" ? "trace-1" : null } });
  };
  try {
    const traceTree = renderSelection("?view=traces&trace_id=trace-1");
    const traceView = findAll(traceTree, (node) => node.type?.name === "Traces")[0];
    assert.equal(traceView.props.selectedTraceId, "trace-1");

    const runTree = renderSelection("?view=agent-runs&run_id=run-1&event_id=event-1");
    const runView = findAll(runTree, (node) => node.type?.name === "Runs")[0];
    assert.equal(runView.props.selectedRunId, "run-1");
    assert.equal(runView.props.routedEventId, "event-1");

    const invalidTree = renderSelection("?view=traces&trace_id=one&trace_id=two");
    assert.match(textOf(invalidTree), /requested Verdict link is invalid/);
    assert.doesNotMatch(textOf(invalidTree), /trace one|trace two/);
  } finally { delete globalThis.window; }
});

test("public query selections survive the loading to live transition", async () => {
  const ui = await loadUiModule();
  const cases = [
    {
      search: "?view=traces&trace_id=trace-1",
      component: "Traces",
      selected: ["selectedTraceId", "trace-1"],
      canonical: "/dashboard#tab=explore&section=calls&trace=trace-1",
    },
    {
      search: "?view=agent-runs&run_id=run-1&event_id=event-1",
      component: "Runs",
      selected: ["selectedRunId", "run-1"],
      canonical: "/dashboard#tab=explore&section=runs&run=run-1&selected=run-1&event_id=event-1",
    },
  ];

  try {
    for (const item of cases) {
      const hooks = createEffectHooks();
      const replacements = [];
      globalThis.window = {
        location: { hash: "", pathname: "/dashboard", search: item.search },
        history: {
          pushState() {},
          replaceState(_state, _unused, destination) { replacements.push(destination); },
        },
        addEventListener() {}, removeEventListener() {},
      };

      render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "loading" });
      hooks.flushEffects();
      const loadedTraceFilter = { traceDate: null, traceJudgeStatus: "all",
        traceId: item.component === "Traces" ? "trace-1" : null };
      render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live", loadedTraceFilter });
      hooks.flushEffects();
      const tree = render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live", loadedTraceFilter });
      const selected = findAll(
        tree,
        (node) => node.type?.name === item.component,
      )[0];
      assert.equal(selected.props[item.selected[0]], item.selected[1]);
      if (item.component === "Runs") assert.equal(selected.props.routedEventId, "event-1");
      assert.equal(replacements.at(-1), item.canonical);
    }
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

test("prototype workspace navigation opens the six task pages through the existing route", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const visited = [];
  globalThis.window = {
    location: { hash: "#tab=overview&section=summary", pathname: "/dashboard", search: "" },
    history: { pushState: (_state, _unused, destination) => visited.push(destination), replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    let tree = render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live", onReload() {} });
    const sidebar = findAll(tree, (node) => node.props?.className?.includes("verdict-workspace-sidebar"))[0];
    assert.ok(sidebar, "prototype workspace sidebar must replace the old horizontal nav");
    const destinations = [
      ["Home", "#tab=overview&section=summary"],
      ["Suggested setup", "#tab=monitor&section=plan"],
      ["Monitoring", "#tab=monitor&section=status"],
      ["Investigation", "#tab=explore&section=runs"],
      ["Groups", "#tab=monitor&section=segments"],
      ["Model drift setup", "#tab=monitor&section=history"],
    ];
    for (const [label, route] of destinations) {
      tree = render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live", onReload() {} });
      const button = findAll(tree, (node) => node.type === "button" && textOf(node).trim() === label)[0];
      assert.ok(button, `${label} must be a direct destination`);
      button.props.onClick();
      assert.equal(visited.at(-1), route);
      if (["Suggested setup", "Monitoring", "Groups", "Model drift setup"].includes(label)) {
        const destination = render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live", onReload() {} });
        const sectionNav = findAll(destination, (node) => node.type === "nav" && node.props?.["aria-label"] === "monitor sections");
        assert.equal(sectionNav.length, 0, `${label} has one primary navigation owner`);
      }
    }
  } finally { delete globalThis.window; }
});

test("investigation link names its monitoring destination regardless of entry path", async () => {
  const ui = await loadUiModule();
  for (const hash of [
    "#tab=explore&section=runs&finding=test_failed&run=run-1&selected=run-1",
    "#tab=explore&section=runs&run=run-1&selected=run-1",
  ]) {
    const destinations = [];
    globalThis.window = {
      location: { hash, pathname: "/dashboard", search: "" },
      history: { pushState: (_state, _unused, target) => destinations.push(target), replaceState() {} },
      addEventListener() {}, removeEventListener() {},
    };
    try {
      const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" });
      const button = findAll(tree, (node) => node.type === "button" && textOf(node).trim() === "View monitoring")[0];
      assert.ok(button, "the destination label must remain truthful without entry-origin state");
      button.props.onClick();
      assert.equal(destinations.at(-1), "#tab=monitor&section=status");
    } finally { delete globalThis.window; }
  }
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
    assert.doesNotMatch(textOf(navigation), /Report/);
    assert.ok(findAll(tree, (node) => node.type === "button" && textOf(node).trim() === "Report").length);
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
    let selectedPeriod = null;
    const tree = render(ui.ManagementReport, createHooks(), {
      data: bundle("judge-a"), source: "live",
      onPeriodChange: (days) => { selectedPeriod = days; },
    });
    const period = findAll(tree, (node) => node.type === "select" && node.props["aria-label"] === "Report period")[0];
    assert.deepEqual(findAll(period, (node) => node.type === "option").map((node) => node.props.value), [7, 30, 90, 0]);
    period.props.onChange({ target: { value: "7" } });
    const buttons = findAll(tree, (node) => node.type === "button");
    buttons.find((button) => textOf(button).includes("Export HTML")).props.onClick();
    buttons.find((button) => textOf(button).includes("Download CSV")).props.onClick();
    buttons.find((button) => textOf(button).includes("Print / Save PDF")).props.onClick();
    assert.equal(downloads.length, 2);
    assert.equal(downloads[0].type, "text/html;charset=utf-8");
    assert.equal(downloads[1].type, "text/csv;charset=utf-8");
    assert.equal(printed, true);
    assert.equal(selectedPeriod, 7);
    const allTime = render(ui.ManagementReport, createHooks(), {
      data: bundle("judge-a", [], 0), source: "live", onPeriodChange: () => {},
    });
    assert.equal(findAll(allTime, (node) => node.type === "select")[0].props.value, 0);
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
  assert.match(text, /Sep 8/);
  assert.match(text, /Sep 10/);
  assert.match(text, /24/);
  assert.match(text, /Application LLM utilization by environment/);
  assert.match(text, /orders-api/);
  assert.match(text, /Model performance and throughput/);
  assert.match(text, /gpt-5-mini/);
  assert.match(text, /480\s+uncached in ·\s+1,920\s+cached in/);
  assert.match(text, /Report scope/);
  assert.doesNotMatch(text, /Capacity & Cost Simulator/);
  assert.doesNotMatch(text, /Recent LLM activity/);
});

test("management report does not render unavailable model tokens as zero", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  Object.assign(data.managementReport.models.rows[0], {
    inputTokens: 0, outputTokens: 0, totalTokens: 0, tokenKnownCalls: 0,
  });
  const tree = render(ui.ManagementReport, createHooks(), { data, source: "live" });
  const modelTable = findAll(
    tree,
    (node) => typeof node.type === "function"
      && node.type.name === "UtilizationTable"
      && node.props.kind === "model",
  )[0];
  const rendered = render(modelTable.type, createHooks(), modelTable.props);
  const tokenCells = findAll(rendered, (node) => node.props?.["data-report-token"]);

  assert.equal(textOf(tokenCells[0]).trim(), "—");
});

test("retired drift links render current Monitor history without the old results", async () => {
  const ui = await loadUiModule();
  let evaluatorLoads = 0;
  globalThis.window = {
    location: { hash: "#tab=monitor&section=signals&evaluator=retired-judge", pathname: "/dashboard" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const data = bundle("judge-a");
    data.driftSignals = [{ id: "retired-signal", dimension: "quality" }];
    data.driftRun = { id: "retired-run", signalCount: 1 };
    const tree = render(ui.Dashboard, createHooks(), {
      data, source: "live", onEvaluatorChange() { evaluatorLoads += 1; },
    });
    const page = findAll(tree,
      (node) => typeof node.type === "function" && node.type.name === "Monitor")[0];
    assert.equal(page.props.view, "history");
    assert.equal(evaluatorLoads, 0);
    assert.doesNotMatch(textOf(tree), /Legacy History|retired-signal|fixed-window/i);
  } finally { delete globalThis.window; }
});

test("guided home keeps historical checks separate from active monitoring", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.monitor = { active: null, candidate: { state: "candidate",
    snapshot: { comparison: { status: "no_alert", metrics: [] } } } };
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data });
    const home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    assert.ok(home);
    const summary = textOf(render(home.type, createHooks(), home.props));
    assert.match(summary, /Known model-call tokens/);
    assert.doesNotMatch(summary, /0\.0% → 0\.0%/);
  } finally { delete globalThis.window; }
});

test("bundled sample keeps its illustrative monitor badge without a live status claim", async () => {
  const ui = await loadUiModule();
  const dashboard = render(ui.Dashboard, createHooks(), {});
  const monitorButton = findAll(dashboard,
    (node) => node.type === "button" && textOf(node).includes("Monitor"))[0];
  assert.equal(textOf(monitorButton).trim(), "Monitoring 1");
  const home = findAll(dashboard, (node) => node.type?.name === "GuidedHome")[0];
  assert.ok(home);
  assert.doesNotMatch(textOf(render(home.type, createHooks(), home.props)), /A monitored change needs review/);
});

test("dashboard shows the six prototype tasks and keeps secondary tools accessible", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a") });
  const navigation = findAll(tree, (node) => node.type === "nav")[0];
  const labels = textOf(navigation);

  for (const label of ["Home", "Suggested setup", "Monitoring", "Investigation", "Groups", "Model drift setup"]) {
    assert.match(labels, new RegExp(label));
  }
  assert.doesNotMatch(labels, /Report|Settings/);
  assert.ok(findAll(tree, (node) => node.type === "button" && textOf(node).trim() === "Report").length);
  assert.ok(findAll(tree, (node) => node.type === "button" && textOf(node).trim() === "Settings").length);
  for (const oldLabel of ["Findings", "Reliability", "Performance", "Behavior", "Agent runs", "Trace explorer", "Judge scores", "Evaluators", "Compare LLMs", "Legacy History"]) {
    assert.doesNotMatch(labels, new RegExp(oldLabel));
  }
});

test("completed local capture leads to the Overview investigation", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  let destination = null;
  globalThis.window = { location: { hash: "#tab=settings&section=sources", pathname: "/dashboard", search: "" },
    history: { pushState: (_state, _title, url) => { destination = url; }, replaceState() {} },
    addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data, source: "live", onReload() {} });
    const setup = findAll(tree, (node) => node.type?.name === "SetupWizard")[0];
    setup.props.onComplete("local");
    assert.equal(destination, "#tab=overview&section=summary");
  } finally { delete globalThis.window; }
});

test("candidate and active evidence links carry their own evaluator identity", async () => {
  const ui = await loadUiModule();
  const opened = [];
  const fingerprintA = "a".repeat(64);
  const fingerprintB = "b".repeat(64);
  const response = (state, fingerprint, traceId) => ({
    state,
    policy: {
      evaluator_fingerprint: fingerprint,
      grouping_mode: "none",
      prospective_target: 10,
    },
    snapshot: {
      manifest: {
        reference_unit_ids: Array(10).fill("reference"),
        current_unit_ids: Array(10).fill("current"),
        prospective_open: false,
        comparison_index: 0,
        reference_summary: {
          metrics: [{ metric: "judge.quality.pass",
            true_unit_ids: [`reference-${traceId}`], false_unit_ids: [] }],
        },
        current_summary: {
          metrics: [{ metric: "judge.quality.pass",
            true_unit_ids: [], false_unit_ids: [traceId] }],
        },
      },
      comparison: {
        status: "alert", alpha_threshold: 0.05,
        metrics: [{ metric: "judge.quality.pass", alert: true,
          reference_value: 1, current_value: 0, effect: -1,
          p_value: 0.001, p_adjusted: 0.001,
          reference_n: 10, current_n: 10 }],
        metric_coverage: [], groups: [], unseen_group_share: 0,
      },
    },
  });
  const tree = render(ui.Monitor, createHooks(), {
    configUrl: "/api/config",
    view: "status",
    evaluation: {
      selectedId: "judge-a-id",
      availableIdentities: [
        { id: "judge-a-id", complete: true, fingerprint: fingerprintA,
          label: "Judge A" },
        { id: "judge-b-id", complete: true, fingerprint: fingerprintB,
          label: "Judge B" },
      ],
    },
    initialState: {
      active: response("active", fingerprintA, "active-trace"),
      candidate: response("candidate", fingerprintB, "candidate-trace"),
    },
    onOpenTrace: (traceId, evaluatorId) => opened.push({ traceId, evaluatorId }),
  });

  const snapshots = findAll(tree,
    (node) => typeof node.type === "function" && node.type.name === "MonitorSnapshot");
  assert.equal(snapshots.length, 2);
  for (const snapshot of snapshots) {
    const snapshotTree = render(snapshot.type, createHooks(), snapshot.props);
    const comparison = findAll(snapshotTree,
      (node) => typeof node.type === "function" && node.type.name === "MonitorComparisonMetrics")[0];
    const comparisonTree = render(comparison.type, createHooks(), comparison.props);
    const card = findAll(comparisonTree,
      (node) => typeof node.type === "function" && node.type.name === "MetricComparisonCard")[0];
    const cardTree = render(card.type, createHooks(), card.props);
    const currentExamples = findAll(cardTree,
      (node) => typeof node.type === "function" && node.type.name === "EvidenceLinks"
        && node.props.label === "Current examples")[0];
    const linksTree = render(currentExamples.type, createHooks(), currentExamples.props);
    findAll(linksTree, (node) => node.type === "button")[0].props.onClick();
  }

  assert.deepEqual(opened, [
    { traceId: "candidate-trace", evaluatorId: "judge-b-id" },
    { traceId: "active-trace", evaluatorId: "judge-a-id" },
  ]);
});

test("monitor trace navigation replaces an unrelated selected evaluator", async () => {
  const ui = await loadUiModule();
  for (const [section, traceId, evaluatorId] of [["history", "candidate-trace", "judge-b-id"]]) {
    let pushed = null;
    globalThis.window = {
      location: {
        hash: `#tab=monitor&section=${section}&evaluator=unrelated-judge`,
        pathname: "/dashboard",
      },
      history: { pushState: (_state, _title, url) => { pushed = url; }, replaceState() {} },
      addEventListener() {}, removeEventListener() {},
    };
    try {
      const tree = render(ui.Dashboard, createHooks(), {
        data: bundle("unrelated-judge"), source: "live",
      });
      const monitor = findAll(tree,
        (node) => typeof node.type === "function" && node.type.name === "Monitor")[0];
      monitor.props.onOpenTrace(traceId, evaluatorId);
      assert.equal(
        pushed,
        `#tab=explore&section=calls&trace=${traceId}&evaluator=${evaluatorId}`,
      );
    } finally {
      delete globalThis.window;
    }
  }
});

test("monitoring status links to comparison history without preserving an unrelated evaluator", async () => {
  const ui = await loadUiModule();
  let pushed = null;
  globalThis.window = {
    location: { hash: "#tab=monitor&section=status&evaluator=unrelated-judge", pathname: "/dashboard" },
    history: { pushState: (_state, _title, url) => { pushed = url; }, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const tree = render(ui.Dashboard, createHooks(), {
      data: bundle("unrelated-judge"), source: "live",
    });
    const inbox = findAll(tree,
      (node) => typeof node.type === "function" && node.type.name === "IssueInbox")[0];
    inbox.props.onOpenHistory();
    assert.equal(pushed, "#tab=monitor&section=history");
    inbox.props.onOpenDeliveries();
    assert.equal(pushed, "#tab=settings&section=alerts");
  } finally {
    delete globalThis.window;
  }
});

test("Trace Explorer renders execution and evaluation as separate states", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "trace-1", provider: "anthropic", request_model: "model-a",
    providerStatus: "no_error_recorded", judgeStatus: "not_judged",
    prompt_redacted: "Question", response_redacted: "Answer",
    input_tokens: 2, output_tokens: 3, started_at: "2026-09-01T00:00:00Z",
  };

  const tree = render(ui.Traces, createHooks(), {
    data: bundle(null, [sample]), source: "live",
  });
  const text = textOf(tree);

  assert.match(text, /Execution/);
  assert.match(text, /Evaluation/);
  assert.match(text, /no provider error recorded/i);
  assert.match(text, /not evaluated/i);
});

test("Compare explains Codex diagnostic model-call coverage", async () => {
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
  assert.match(textOf(tree), /metadata-only model calls/i);
  assert.match(textOf(tree), /token usage where a matching session event exists/i);
  assert.match(textOf(tree), /Agent-run and model-call counts can differ/i);
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
  const hooks = createHooks();
  const props = {
    configUrl: "/api/config",
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
  };
  let tree = render(ui.RegistryView, hooks, props);
  const rendered = textOf(tree).replace(/\s+/g, " ");

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
  const cluster = findAll(tree, (node) => node.type === "select"
    && node.props["aria-label"] === "Cluster to rename")[0];
  cluster.props.onChange({ target: { value: "clu-a" } });
  tree = render(ui.RegistryView, hooks, props);
  const assist = findAll(tree, (node) => node.type?.name === "EvidenceExplain"
    && node.props.draftKind === "cluster_description")[0];
  assert.ok(assist);
  assert.equal(assist.props.versionId, "crv-preview");
  assert.equal(assist.props.clusterId, "clu-a");
  assist.props.onApplyDraft({ displayName: "Billing assistance", description: "Billing requests.", why: "Examples." });
  tree = render(ui.RegistryView, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "input"
    && node.props["aria-label"] === "Cluster display name")[0].props.value, "Billing assistance");
  assert.deepEqual(actions, []);
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

test("a synthetic local store is labeled as demo data throughout the dashboard", async () => {
  const ui = await loadUiModule();
  globalThis.window = {
    location: { hash: "#tab=explore&section=runs", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const data = bundle("judge-a");
    data.meta.agentRunSources = [{ sourceKind: "synthetic-codex", runs: 1 }];
    data.meta.dataProvenance = "synthetic";
    const tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    assert.match(textOf(tree), /Synthetic local demo/);
    assert.match(textOf(tree), /ILLUSTRATIVE SYNTHETIC DATA/);
  } finally { delete globalThis.window; }
});

test("explicit synthetic provenance labels mixed demo stores without a synthetic source kind", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.meta.agentRunSources = [{ sourceKind: "verdict_sdk", runs: 1 }];
  data.meta.dataProvenance = "synthetic";
  const rendered = textOf(render(ui.Dashboard, createHooks(), { data, source: "live" }));
  assert.match(rendered, /Synthetic demo/);
  assert.match(rendered, /ILLUSTRATIVE SYNTHETIC DATA/);
  assert.doesNotMatch(rendered, /Live store/);
});

test("a real store with some synthetic-codex records is not labeled as a synthetic demo", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.meta.agentRunSources = [
    { sourceKind: "synthetic-codex", runs: 1 },
    { sourceKind: "verdict_sdk", runs: 3 },
  ];
  data.meta.dataProvenance = "unspecified";
  const rendered = textOf(render(ui.Dashboard, createHooks(), { data, source: "live" }));
  assert.match(rendered, /Connected store/);
  assert.doesNotMatch(rendered, /Synthetic demo|ILLUSTRATIVE SYNTHETIC DATA/);
});

test("Groups gives model-call coverage and does not present a synthetic marker as a model", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.Registry, createHooks(), {
    url: "/api/registry", configUrl: "/api/config", traceCount: 7,
    modelRows: [{ provider: "anthropic", model: "<synthetic>", calls: 7,
      failedCalls: 2, tokenKnownCalls: 3 }],
    modelRowsAvailable: 1,
  });
  const rendered = textOf(tree).replace(/\s+/g, " ");
  assert.match(rendered, /Synthetic model marker/);
  assert.match(rendered, /2 calls with recorded provider errors/);
  assert.match(rendered, /3 of 7 calls with token totals/);
  assert.doesNotMatch(rendered, /anthropic \/ <synthetic>/);
});

test("Groups intro follows recorded source eligibility", async () => {
  const ui = await loadUiModule();
  globalThis.window = {
    location: { hash: "#tab=monitor&section=segments", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const runsOnly = bundle(null);
    runsOnly.meta.totalAgentRuns = 3;
    const noCalls = textOf(render(ui.Dashboard, createHooks(), { data: runsOnly, source: "live" }));
    assert.match(noCalls, /Start with recorded sources and agent names/);
    assert.doesNotMatch(noCalls, /Start with recorded source and model fields/);
    const withCalls = bundle(null);
    withCalls.meta.totalAgentRuns = 3;
    withCalls.meta.totalTraces = 2;
    const calls = textOf(render(ui.Dashboard, createHooks(), { data: withCalls, source: "live" }));
    assert.match(calls, /Start with recorded source and model fields/);
  } finally { delete globalThis.window; }
});

test("Groups shows production run outcome coverage with exact evidence links", async () => {
  const ui = await loadUiModule();
  const opened = [];
  const tree = render(ui.RunOutcomeSummary, createHooks(), {
    data: {
      status: "ready",
      scope: { terminalRuns: 5, sampledTerminalRuns: 5, truncated: false },
      totals: { recordedFailure: 2, observedWithoutFailure: 1,
        unclear: 2, conflicts: 1 },
      groups: [{ sourceKind: "verdict_sdk", serviceName: "app", environment: "prod",
        agentName: "worker", terminalRuns: 5, recordedFailure: 2,
        observedWithoutFailure: 1, unclear: 2, conflicts: 1,
        comparison: {
          earlier: { terminalRuns: 2, recordedFailure: 1,
            observedWithoutFailure: 1, unclear: 0 },
          newer: { terminalRuns: 3, recordedFailure: 1,
            observedWithoutFailure: 0, unclear: 2 },
        },
        examples: { recordedFailure: ["run-failed"],
          observedWithoutFailure: ["run-ok"], unclear: ["run-unknown"] } }],
    },
    onOpenRun: (id) => opened.push(id),
  });
  const rendered = textOf(tree).replace(/\s+/g, " ");
  assert.match(rendered, /Recorded tool-result coverage by run/);
  assert.match(rendered, /2 recorded tool-result failures/);
  assert.match(rendered, /Run-status and test failures appear in Monitoring/);
  assert.match(rendered, /1 observed without recorded failure/);
  assert.match(rendered, /A completed result with no unclear evidence/);
  assert.match(rendered, /When calls are recorded, each has a matching result/);
  assert.doesNotMatch(rendered, /Recorded calls have matching results/);
  assert.match(rendered, /observed without recorded failure/);
  assert.doesNotMatch(rendered, /observed without failure/);
  assert.match(rendered, /2 unclear/);
  assert.match(rendered, /not task success/);
  assert.match(rendered, /Earlier 2 runs: 1 recorded tool failure\s*, 0 unclear/);
  assert.match(rendered, /Newer 3 runs: 1 recorded tool failure\s*, 2 unclear/);
  assert.match(rendered, /Descriptive split only/);
  assert.doesNotMatch(rendered, /run-failed|run-ok|run-unknown/);
  const inspect = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Inspect recorded failure"))[0];
  inspect.props.onClick();
  assert.deepEqual(opened, ["run-failed"]);
});

test("Groups with an unmigrated read-only store withholds outcome counts", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.RunOutcomeSummary, createHooks(), {
    data: { status: "migration_required", totals: null, groups: [] },
  });
  assert.match(textOf(tree), /writable Verdict storage upgrade/);
  assert.match(textOf(tree), /No outcome counts are shown/);
});

test("Groups gives an unfinalized-only sample one actionable coverage summary", async () => {
  const ui = await loadUiModule();
  let browsed = 0;
  const opened = [];
  const tree = render(ui.RunOutcomeSummary, createHooks(), {
    data: {
      status: "no_terminal_runs", scope: { sampledTerminalRuns: 0, truncated: false },
      execution: {
        scope: { sampledRuns: 2, truncated: true },
        totals: { completed: 0, failed: 0, timed_out: 0,
          cancelled: 0, unfinalized: 2 },
        groups: [{ sourceKind: "codex", serviceName: "", environment: "",
          agentName: "local-agent", counts: { completed: 0, failed: 0,
            timed_out: 0, cancelled: 0, unfinalized: 2 },
          examples: { completed: [], failed: [], timed_out: [],
            cancelled: [], unfinalized: ["history-run"] } }],
        groupsTruncated: false,
      },
    },
    onRuns: () => { browsed += 1; },
    onOpenRun: (id) => opened.push(id),
  });
  const rendered = textOf(tree).replace(/\s+/g, " ");
  assert.match(rendered, /2 recent recorded runs have no terminal status with an end time/);
  assert.match(rendered, /older runs are omitted/);
  assert.match(rendered, /For agents you control, record a terminal status and end time to compare run outcomes/);
  assert.doesNotMatch(rendered, /Recorded execution status|Recorded tool-result coverage by run/);
  assert.doesNotMatch(rendered, /history-run|statistical alert active|0 completed/);
  const browse = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Browse recorded runs"))[0];
  assert.ok(browse);
  browse.props.onClick();
  assert.equal(browsed, 1);
  const inspect = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Inspect example run"))[0];
  inspect.props.onClick();
  assert.deepEqual(opened, ["history-run"]);
});

test("Groups loads an unfinalized-only preview through its API path and opens runs", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  let browsed = 0;
  const props = { configUrl: "/mounted/api/config", onRuns: () => { browsed += 1; } };
  render(ui.RunOutcomePreview, hooks, props); hooks.flushEffects();
  assert.equal(requests[0].url, "/mounted/api/agent-run-outcomes");
  await resolveJson(requests[0], { status: "no_terminal_runs", execution: {
    scope: { sampledRuns: 1, truncated: false }, totals: { unfinalized: 1 },
    groups: [], groupsTruncated: false,
  } });
  const preview = render(ui.RunOutcomePreview, hooks, props);
  const summary = findAll(preview, (node) => node.type?.name === "RunOutcomeSummary")[0];
  assert.ok(summary);
  const rendered = render(summary.type, createHooks(), summary.props);
  const browse = findAll(rendered, (node) => node.type === "button"
    && textOf(node).includes("Browse recorded runs"))[0];
  browse.props.onClick();
  assert.equal(browsed, 1);
});

test("Groups with no runs shows capture guidance and no browse action", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.RunOutcomeSummary, createHooks(), {
    data: { status: "no_terminal_runs", execution: { scope: { sampledRuns: 0 } } },
    onRuns: () => assert.fail("no runs to browse"),
  });
  assert.match(textOf(tree), /No Agent Runs have been captured yet/);
  assert.doesNotMatch(textOf(tree), /Browse recorded runs|older runs are omitted/);
});

test("Groups keeps a bounded event-limit warning when terminal runs exist", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.RunOutcomeSummary, createHooks(), {
    data: { status: "event_limit", execution: {
      scope: { sampledRuns: 1, truncated: false },
      totals: { completed: 1 }, groups: [], groupsTruncated: false,
    } },
    onRuns: () => assert.fail("event-limit state is not a browse-only state"),
  });
  const rendered = textOf(tree);
  assert.match(rendered, /Recorded execution status/);
  assert.match(rendered, /reached its relevant-event limit/);
  assert.doesNotMatch(rendered, /Run outcome coverage|Browse recorded runs/);
});

test("a recorded failure on an issue-only UTC day opens its exact run", async () => {
  const ui = await loadUiModule();
  const selected = [];
  const chart = render(ui.UsageChart, createEffectHooks(), {
    days: [{ day: "2026-10-18", turns: 0, totalTokens: 0,
      recordedIssueEvents: 1 }],
    selectedDay: "2026-10-18", measure: "tokens",
    onSelectDay: (day) => selected.push(day),
  });
  const bar = findAll(chart, (node) => node.type === "button"
    && node.props["aria-label"]?.includes("recorded issue event"))[0];
  assert.ok(bar);
  bar.props.onClick();
  assert.deepEqual(selected, ["2026-10-18"]);
  const opened = [];
  const detail = render(ui.DayIssueSummary, createHooks(), {
    day: "2026-10-18",
    issues: { complete: true, count: 1, events: [{ code: "tool_error",
      runId: "run-alice@example.com", eventId: "event-1" }],
      eventsTruncated: false },
    onOpenRun: (runId, eventId) => opened.push([runId, eventId]),
  });
  assert.match(textOf(detail), /based on event time/);
  assert.doesNotMatch(textOf(detail), /alice@example.com/);
  findAll(detail, (node) => node.type === "button")[0].props.onClick();
  assert.deepEqual(opened, [["run-alice@example.com", "event-1"]]);
});

test("a source-partial day with no captured turns or issues remains selectable", async () => {
  const ui = await loadUiModule();
  const selected = [];
  const chart = render(ui.UsageChart, createEffectHooks(), {
    days: [{ day: "2026-10-18", turns: 0, totalTokens: 0,
      sourcePartial: true, issueSourcePartial: true, recordedIssueEvents: 0 }],
    selectedDay: "2026-10-17", measure: "tokens",
    onSelectDay: (day) => selected.push(day),
  });
  const bar = findAll(chart, (node) => node.type === "button"
    && node.props["aria-label"]?.includes("2026-10-18"))[0];
  assert.ok(bar);
  bar.props.onClick();
  assert.deepEqual(selected, ["2026-10-18"]);
});

test("an event-only source cap warns about issues without calling token totals partial", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const report = { window: { complete: false, turnsComplete: true,
    modelEvidenceComplete: true },
  days: [{ day: "2026-10-13", turns: 1, tokenKnownTurns: 1,
    totalTokens: 900, pricedTurns: 0, sourcePartial: false,
    issueSourcePartial: true, recordedIssueEvents: 0 }],
  selectedDay: "2026-10-13", selectedDayIssues: { complete: false,
    count: null, observedCount: 0, sourcePartial: true, events: [],
    eventsTruncated: false }, dayModels: [], daySources: [], sessions: [],
  sessionsTruncated: false, selectedSession: null };
  render(ui.Usage, hooks, { url: "/api/usage", compact: true, home: true });
  hooks.flushEffects();
  await resolveJson(requests[0], report);
  const tree = render(ui.Usage, hooks, { url: "/api/usage", compact: true, home: true });
  assert.match(textOf(tree), /Local issue-event history is incomplete or unverified/i);
  assert.doesNotMatch(textOf(tree), /recorded token totals may omit source activity/i);
});

test("Home opens an empty captured day whose issue coverage is partial", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const report = { window: { complete: false, turnsComplete: true,
    modelEvidenceComplete: true },
    days: [{ day: "2026-10-13", turns: 0, tokenKnownTurns: 0,
      totalTokens: 0, pricedTurns: 0, issueSourcePartial: true,
      recordedIssueEvents: 0 }],
    selectedDay: "2026-10-13", selectedDayIssues: { complete: false,
      count: null, observedCount: 0, sourcePartial: true, events: [],
      eventsTruncated: false }, dayModels: [], daySources: [], sessions: [],
    sessionsTruncated: false, selectedSession: null };
  const props = { url: "/api/usage", compact: true, home: true };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], report);
  const tree = render(ui.Usage, hooks, props);
  const inspect = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("Inspect incomplete day"))[0];
  assert.ok(inspect);
  inspect.props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  assert.match(requests[1].url, /day=2026-10-13/);
  await resolveJson(requests[1], report);
  const detail = render(ui.Usage, hooks, props);
  const facts = findAll(detail, (node) => node.type?.name === "Fact");
  assert.equal(facts[0].props.value, "No captured token report");
  assert.equal(facts[1].props.value, "Not priced");
});

test("an explicitly reported zero-token turn keeps numeric zero in day facts", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const report = { window: { complete: true, turnsComplete: true,
    modelEvidenceComplete: true },
    days: [{ day: "2026-10-13", turns: 1, tokenKnownTurns: 1,
      totalTokens: 0, pricedTurns: 1, listPriceUsd: 0,
      unattributedTokens: 0, recordedIssueEvents: 0 }],
    selectedDay: "2026-10-13", selectedDayIssues: { complete: true,
      count: 0, events: [], eventsTruncated: false },
    dayModels: [], daySources: [], sessions: [], sessionsTruncated: false,
    selectedSession: null };
  render(ui.Usage, hooks, { url: "/api/usage", compact: false });
  hooks.flushEffects();
  await resolveJson(requests[0], report);
  const detail = render(ui.Usage, hooks, { url: "/api/usage", compact: false });
  const facts = findAll(detail, (node) => node.type?.name === "Fact");
  assert.equal(facts[0].props.value, "0 reported tokens");
  assert.equal(facts[1].props.value, "$0.00");
  assert.equal(facts[2].props.value, "0");
});

test("partial source issue day shows a lower bound and keeps exact evidence links", async () => {
  const ui = await loadUiModule();
  const opened = [];
  const detail = render(ui.DayIssueSummary, createHooks(), {
    day: "2026-10-18",
    issues: { complete: false, count: null, observedCount: 1, sourcePartial: true,
      events: [{ code: "test_failed", runId: "run-1", eventId: "event-1" }],
      eventsTruncated: false },
    onOpenRun: (runId, eventId) => opened.push([runId, eventId]),
  });
  assert.match(textOf(detail), /At least 1 recorded issue event/);
  assert.match(textOf(detail), /source history is partial/);
  findAll(detail, (node) => node.type === "button")[0].props.onClick();
  assert.deepEqual(opened, [["run-1", "event-1"]]);
  const noObservedIssues = render(ui.DayIssueSummary, createHooks(), {
    day: "2026-10-18",
    issues: { complete: false, count: null, observedCount: 0, sourcePartial: true,
      events: [], eventsTruncated: false },
  });
  assert.match(textOf(noObservedIssues), /No recorded issue events in captured evidence/);
  assert.match(textOf(noObservedIssues), /full-day count is unknown/);
});

test("a retained recent issue day passes its first-seen event key to Investigation", async () => {
  const ui = await loadUiModule();
  const opened = [];
  const revision = "a".repeat(64);
  const detail = render(ui.DayIssueSummary, createHooks(), {
    day: "2026-10-18",
    issues: { complete: false, count: null, observedCount: 1, sourcePartial: true,
      events: [{ code: "command_failed", runId: "run-1", eventId: "late-event",
        recentRevision: revision, recentTurnId: "late-turn" }],
      eventsTruncated: false },
    onOpenRun: (...args) => opened.push(args),
  });
  findAll(detail, (node) => node.type === "button")[0].props.onClick();
  assert.deepEqual(opened, [["run-1", "late-event", revision, "late-turn"]]);
});

test("Home routes retained day evidence with exact keys and clears them for regular events", async () => {
  const ui = await loadUiModule();
  const pushed = [];
  globalThis.window = { location: { hash: "#tab=overview&section=summary",
    pathname: "/dashboard", search: "" },
    history: { pushState(_state, _title, url) { pushed.push(url); }, replaceState() {} },
    addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" });
    const home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    const revision = "a".repeat(64);
    home.props.onOpenRun("run-1", "late-event", revision, "late-turn");
    assert.match(pushed.at(-1), /recent_rev=a{64}/);
    assert.match(pushed.at(-1), /recent_turn=late-turn/);
    assert.match(pushed.at(-1), /recent_event=late-event/);
    home.props.onOpenRun("run-2", "regular-event");
    assert.match(pushed.at(-1), /event_id=regular-event/);
    assert.doesNotMatch(pushed.at(-1), /recent_rev|recent_turn|recent_event/);
  } finally { delete globalThis.window; }
});

test("Groups retries failed run outcomes and refreshes newly captured runs", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", onOpenRun: () => {} };
  render(ui.RunOutcomePreview, hooks, props);
  hooks.flushEffects();
  assert.equal(requests[0].url, "/api/agent-run-outcomes");
  requests[0].resolve({ ok: false, status: 503 });
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
  let tree = render(ui.RunOutcomePreview, hooks, props);
  assert.match(textOf(tree), /could not be loaded/);
  findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Retry run outcomes")[0].props.onClick();
  render(ui.RunOutcomePreview, hooks, props);
  hooks.flushEffects();
  assert.equal(requests.length, 2);
  await resolveJson(requests[1], { status: "no_terminal_runs" });
  tree = render(ui.RunOutcomePreview, hooks, props);
  assert.match(textOf(tree), /Refresh run outcomes/);
  findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Refresh run outcomes")[0].props.onClick();
  render(ui.RunOutcomePreview, hooks, props);
  hooks.flushEffects();
  assert.equal(requests.length, 3);
});

async function resolveJson(request, payload) {
  request.resolve({ ok: true, json: async () => payload });
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

test("AI result disappears when the selected run changes", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", scope: "run", runId: "run-a" };
  let tree = render(ui.EvidenceExplain, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Explain this run"))[0].props.onClick();
  render(ui.EvidenceExplain, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "test-token" });
  await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: true }] });
  tree = render(ui.EvidenceExplain, hooks, props);
  const previewAction = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Preview evidence"))[0].props.onClick();
  await resolveJson(requests[2], { plannedCalls: 1, destination: "Anthropic", planFingerprint: "plan-a",
    providerAvailable: true, upperStaticListPriceUsd: 0.001, evidencePreview: { coverage: "complete" } });
  await previewAction;
  tree = render(ui.EvidenceExplain, hooks, props);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.EvidenceExplain, hooks, props);
  const runAction = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Explain selected evidence"))[0].props.onClick();
  await resolveJson(requests[3], { status: "completed", answer: "Answer for run A" });
  await runAction;
  assert.match(textOf(render(ui.EvidenceExplain, hooks, props)), /Answer for run A/);
  assert.doesNotMatch(textOf(render(ui.EvidenceExplain, hooks, { ...props, runId: "run-b" })), /Answer for run A/);
  hooks.flushEffects();
  tree = render(ui.EvidenceExplain, hooks, props);
  assert.doesNotMatch(textOf(tree), /Answer for run A/);
  assert.equal(findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox").length, 0);
});

test("AI explanation cannot be approved when the provider SDK is unavailable", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", scope: "run", runId: "synthetic-run" };
  let tree = render(ui.EvidenceExplain, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Explain this run"))[0].props.onClick();
  render(ui.EvidenceExplain, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "test-token" });
  await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: true }] });
  tree = render(ui.EvidenceExplain, hooks, props);
  const preview = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Preview evidence"))[0].props.onClick();
  await resolveJson(requests[2], { plannedCalls: 1, destination: "Anthropic", planFingerprint: "plan",
    providerAvailable: false, upperStaticListPriceUsd: 0.001, evidencePreview: { coverage: "complete" } });
  await preview;
  tree = render(ui.EvidenceExplain, hooks, props);
  const consent = findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0];
  assert.equal(consent.props.disabled, true);
  assert.equal(findAll(tree, (node) => node.type === "button" && textOf(node).includes("Explain selected evidence"))[0].props.disabled, true);
  assert.match(textOf(tree), /SDK installed/);
  assert.equal(requests.length, 3);
});

test("withheld AI previews explain coverage limits without exposing reason codes", async () => {
  const ui = await loadUiModule();
  for (const [reason, expected] of [
    ["source_capture_partial", /Some events in this run were omitted/],
    ["turn_content_partial", /request or response evidence is incomplete/],
    ["run_in_progress", /run is still in progress/],
    ["evidence_limit", /bounded AI context/],
    ["unexpected_private_reason", /selected evidence is incomplete or unavailable/],
    ["__proto__", /selected evidence is incomplete or unavailable/],
    ["constructor", /selected evidence is incomplete or unavailable/],
  ]) {
    const hooks = createEffectHooks();
    const requests = deferredFetches();
    const props = { root: "", scope: "turn", runId: "synthetic-run", turnId: "turn-a" };
    let tree = render(ui.EvidenceExplain, hooks, props);
    findAll(tree, (node) => node.type === "button" && textOf(node).includes("Explain this turn"))[0]
      .props.onClick();
    render(ui.EvidenceExplain, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], { setupToken: "local-token" });
    await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: true }] });
    tree = render(ui.EvidenceExplain, hooks, props);
    const action = findAll(tree, (node) => node.type === "button"
      && textOf(node).includes("Preview evidence"))[0].props.onClick();
    await resolveJson(requests[2], { plannedCalls: 0, destination: "Anthropic",
      providerAvailable: true, upperStaticListPriceUsd: null,
      evidencePreview: { coverage: "partial", reason } });
    await action;
    tree = render(ui.EvidenceExplain, hooks, props);
    assert.match(textOf(tree), expected);
    assert.doesNotMatch(textOf(tree), new RegExp(reason));
    assert.equal(findAll(tree, (node) => node.type === "button"
      && textOf(node).includes("Explain selected evidence")).length, 0);
    assert.equal(findAll(tree, (node) => node.type === "input"
      && node.props.type === "checkbox").length, 0);
    assert.equal(requests.length, 3);
  }
});

test("scoped evaluation requires an exact preview and new approval after criterion changes", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", scope: "run", runId: "synthetic-run" };
  let tree = render(ui.ScopedEvaluate, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Evaluate this run"))[0]
    .props.onClick();
  render(ui.ScopedEvaluate, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "local-token" });
  await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: true }] });
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.equal(requests.length, 2, "opening the panel must not judge");
  const start = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Preview bounded evidence"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).runId, "synthetic-run");
  await resolveJson(requests[2], { coverage: "complete", plannedCalls: 1, provider: "anthropic",
    model: "claude-haiku-4-5", destination: "https://api.anthropic.com",
    estimatedMaximumCostUsd: 0.01, planFingerprint: "approved-plan", evidencePreview: { query: "synthetic task" } });
  await start;
  assert.equal(requests[3].url, "/api/scoped/scheduled");
  await resolveJson(requests[3], { jobs: [] });
  tree = render(ui.ScopedEvaluate, hooks, props);
  const runButton = () => findAll(tree, (node) => node.type === "button" && textOf(node).includes("Run evaluation"))[0];
  assert.equal(runButton().props.disabled, true);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.equal(runButton().props.disabled, false);
  findAll(tree, (node) => node.type === "textarea")[0].props.onChange({ target: { value: "Different criterion" } });
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.equal(runButton().props.disabled, true);
  assert.equal(requests.length, 4);
});

test("one-time scheduled evaluation requires a visible budget and persistent consent", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", scope: "session", sessionId: "synthetic-session", sourceKind: "synthetic-codex" };
  let tree = render(ui.ScopedEvaluate, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Evaluate this session"))[0].props.onClick();
  render(ui.ScopedEvaluate, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "local-token" });
  await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: false }] });
  tree = render(ui.ScopedEvaluate, hooks, props);
  const previewing = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Preview bounded evidence"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).sessionId, "synthetic-session");
  await resolveJson(requests[2], { coverage: "complete", plannedCalls: 1, provider: "anthropic",
    model: "claude-haiku-4-5", destination: "https://api.anthropic.com",
    estimatedMaximumCostUsd: 0.002, scheduledUpperStaticListPriceUsd: 0.01,
    planFingerprint: "approved-session", evidencePreview: { query: "synthetic task" } });
  await previewing;
  await resolveJson(requests[3], { jobs: [] });
  tree = render(ui.ScopedEvaluate, hooks, props);
  const queue = () => findAll(tree, (node) => node.type === "button" && textOf(node).includes("Queue one evaluation"))[0];
  assert.equal(queue().props.disabled, true);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[1]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.equal(queue().props.disabled, false);
  const queuing = queue().props.onClick();
  assert.equal(requests[4].url, "/api/scoped/schedule");
  assert.equal(JSON.parse(requests[4].options.body).confirmPersistentExternalEgress, true);
  await resolveJson(requests[4], { jobId: "synthetic-job", state: "pending", revision: 1 });
  await queuing;
  tree = render(ui.ScopedEvaluate, hooks, props);
  const cancelling = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Cancel approval"))[0].props.onClick();
  assert.equal(requests[5].url, "/api/scoped/scheduled/synthetic-job/cancel");
  await resolveJson(requests[5], { jobId: "synthetic-job", state: "cancelled", revision: 2 });
  await cancelling;
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.match(textOf(tree), /Scheduled approval:\s+cancelled/);
  assert.equal(queue().props.disabled, true, "requeue requires fresh consent");
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[1]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.ScopedEvaluate, hooks, props);
  const requeuing = queue().props.onClick();
  assert.equal(requests[6].url, "/api/scoped/schedule");
  await resolveJson(requests[6], { jobId: "second-job", state: "pending", revision: 1 });
  await requeuing;
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.match(textOf(tree), /Scheduled approval:\s+pending/);
});

test("spent scheduled approval opens its saved result without another judge request", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", scope: "run", runId: "synthetic-run" };
  let tree = render(ui.ScopedEvaluate, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Evaluate this run"))[0].props.onClick();
  render(ui.ScopedEvaluate, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "local-token" });
  await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: false }] });
  tree = render(ui.ScopedEvaluate, hooks, props);
  const previewing = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview bounded evidence"))[0].props.onClick();
  await resolveJson(requests[2], { coverage: "complete", plannedCalls: 1, provider: "anthropic",
    model: "claude-haiku-4-5", destination: "https://api.anthropic.com",
    scheduledUpperStaticListPriceUsd: 0.01, planFingerprint: "saved-plan",
    evidencePreview: { query: "synthetic task" } });
  await previewing;
  await resolveJson(requests[3], { jobs: [{ jobId: "saved-job", state: "spent",
    planFingerprint: "saved-plan", outcome: "completed", resultId: "saved-result" }] });
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.match(textOf(tree), /Scheduled approval:\s+spent/);
  const opening = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Open saved result"))[0].props.onClick();
  assert.equal(requests[4].url, "/api/scoped/result/saved-result");
  assert.equal(requests[4].options?.method, undefined);
  await resolveJson(requests[4], { status: "completed", coverage: "complete",
    evaluatorFingerprint: "synthetic-fingerprint", dimensions: [{ name: "completion",
      verdict: "pass", citationStatus: "linked", explanation: "[turn:turn-a] supports it",
      evidenceReferences: ["turn:turn-a"] }] });
  await opening;
  tree = render(ui.ScopedEvaluate, hooks, props);
  assert.match(textOf(tree), /completion\s*:\s+pass/);
  assert.equal(requests.length, 5);
});

test("saved case comparison requires reviewed inputs and an explicitly configured runner", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", token: "local-token" };
  render(ui.Experiments, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { caseSets: [], configuredVariants: [], runnerConfigured: false });
  let tree = render(ui.Experiments, hooks, props);
  const save = () => findAll(tree, (node) => node.type === "button" && textOf(node).includes("Save reviewed case set"))[0];
  const run = () => findAll(tree, (node) => node.type === "button" && textOf(node).includes("Run baseline and candidate"))[0];
  assert.equal(save().props.disabled, true);
  assert.equal(run().props.disabled, true);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.Experiments, hooks, props);
  assert.equal(save().props.disabled, false);
  assert.equal(run().props.disabled, true);
  assert.equal(requests.length, 1);
});

test("saved cases disclose verbatim storage and block a combination beyond the runner limit", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", token: "local-token" };
  render(ui.Experiments, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], {
    caseSets: [{ documentId: "synthetic", state: "active", revision: 1,
      payload: { cases: [{ id: "one" }, { id: "two" }] } }],
    configuredVariants: ["baseline", "candidate"], runnerConfigured: true,
    trialTimeoutSeconds: 120, maxRunSeconds: 600, experiments: [],
  });
  let tree = render(ui.Experiments, hooks, props);
  const run = () => findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run baseline and candidate"))[0];
  assert.equal(run().props.disabled, false);
  assert.match(textOf(tree), /stored and passed to the configured local runner exactly as entered/);
  assert.match(textOf(tree), /480\s+of\s+600\s+seconds/);
  findAll(tree, (node) => node.type === "select")[4]
    .props.onChange({ target: { value: "3" } });
  tree = render(ui.Experiments, hooks, props);
  assert.equal(run().props.disabled, true);
  assert.match(textOf(tree), /Use fewer cases or trials/);
  assert.equal(requests.length, 1);
});

test("saved experiment comparison reopens after reload without executing the runner", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "", token: "local-token" };
  render(ui.Experiments, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { caseSets: [], configuredVariants: ["baseline", "candidate"],
    runnerConfigured: true, trialTimeoutSeconds: 45, experiments: [{
      documentId: "saved-1", createdAt: "2026-10-13T12:00:00Z", state: "completed",
      payload: { caseSetId: "synthetic", caseSetRevision: 2,
        baseline: "baseline", candidate: "candidate", baselineVersion: "v1",
        candidateVersion: "v2", evaluatorIdentity: "deterministic-output-checks-v1",
        configurationFingerprint: "a".repeat(64), trialsPerSide: 1,
        comparison: { fixed: ["case-a"], newRegressions: [], unchangedPasses: [],
          unchangedFailures: [], unverified: [] },
        cases: [{ id: "case-a", expectedBehavior: "fix the synthetic task",
          baseline: { status: "fail", trials: [{ status: "fail", output: "before" }] },
          candidate: { status: "pass", trials: [{ status: "pass", output: "after" }] },
          comparableMetrics: { taskElapsedMs: null, reportedCostUsd: null } },
          { id: "case-large", expectedBehavior: "bounded runner input",
            baseline: { status: "unverified", trials: [{ status: "error", error: "input_too_large" }] },
            candidate: { status: "unverified", trials: [{ status: "error", error: "input_too_large" }] },
            comparableMetrics: { taskElapsedMs: null, reportedCostUsd: null } }],
        claim: "Fixed-case comparison only." },
    }] });
  const tree = render(ui.Experiments, hooks, props);
  assert.match(textOf(tree), /Fixed-case comparison/);
  assert.match(textOf(tree), /case-a/);
  assert.match(textOf(tree), /deterministic-output-checks-v1/);
  assert.match(textOf(tree), /Input too large for local runner/);
  assert.equal(requests.length, 1);
});

test("captured run drafts only prefill complete redacted input and require review", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const draft = { runId: "run-a", turnId: "turn-1", request: "redacted task", complete: true };
  render(ui.Experiments, hooks, { root: "", token: "local-token", draft });
  hooks.flushEffects();
  await resolveJson(requests[0], { caseSets: [], configuredVariants: [], runnerConfigured: false });
  let tree = render(ui.Experiments, hooks, { root: "", token: "local-token", draft });
  assert.match(textOf(tree), /Draft from captured run\s+run-a\s*, turn turn-1/);
  assert.equal(findAll(tree, (node) => node.type === "textarea")[0].props.value, "redacted task");
  assert.equal(findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Save reviewed case set"))[0].props.disabled, true);
  assert.equal(findAll(tree, (node) => node.type === "input" &&
    node.props.type === "checkbox")[0].props.checked, false);

  const missing = { runId: "run-b", turnId: null, request: "truncated", complete: false };
  render(ui.Experiments, hooks, { root: "", token: "local-token", draft: missing });
  hooks.flushEffects();
  tree = render(ui.Experiments, hooks, { root: "", token: "local-token", draft: missing });
  assert.equal(findAll(tree, (node) => node.type === "textarea")[0].props.value, "");
  assert.match(textOf(tree), /No complete captured request was available/);
});

test("Saved Cases has its own Evaluate route and obtains local authorization", async () => {
  const ui = await loadUiModule();
  globalThis.window = {
    location: { hash: "#tab=evaluate&section=cases", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {}, removeEventListener() {},
  };
  try {
    const dashboard = render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" });
    assert.equal(findAll(dashboard, (node) => node.type?.name === "Experiments").length, 1);
  } finally { delete globalThis.window; }
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { root: "" };
  render(ui.Experiments, hooks, props);
  hooks.flushEffects();
  assert.equal(requests.length, 2);
  await resolveJson(requests[0], { caseSets: [], configuredVariants: [], runnerConfigured: false });
  await resolveJson(requests[1], { setupToken: "local-token" });
  let tree = render(ui.Experiments, hooks, props);
  findAll(tree, (node) => node.type === "input" && node.props.type === "checkbox")[0]
    .props.onChange({ target: { checked: true } });
  tree = render(ui.Experiments, hooks, props);
  assert.equal(findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Save reviewed case set"))[0].props.disabled, false);
});

test("matched conversation comparison uses current paired grades and rejects stale drilldown", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", source: "live" };
  let tree = render(ui.MatchedConversationCompare, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  const values = { windowStart: "2026-09-01", windowEnd: "2026-09-04",
    evaluatorFingerprint: "a".repeat(64), dimension: "quality",
    leftVariant: "left", rightVariant: "right" };
  const labels = { windowStart: "Start date (UTC, inclusive)", windowEnd: "End date (UTC, exclusive)",
    evaluatorFingerprint: "Evaluator fingerprint", dimension: "Rubric dimension",
    leftVariant: "First variant value", rightVariant: "Second variant value" };
  for (const [field, value] of Object.entries(values)) {
    tree = render(ui.MatchedConversationCompare, hooks, props);
    findAll(tree, (node) => node.type === "input" && node.props["aria-label"] === labels[field])[0]
      .props.onChange({ target: { value } });
  }
  tree = render(ui.MatchedConversationCompare, hooks, props);
  const comparing = findAll(tree, (node) => node.type === "form")[0]
    .props.onSubmit({ preventDefault() {} });
  assert.equal(requests[1].url, "/api/compare/conversations/matched");
  assert.equal(requests[1].options.headers["X-Verdict-Setup"], "setup-token");
  assert.equal(JSON.parse(requests[1].options.body).windowEnd, "2026-09-04T00:00:00Z");
  const left = { id: "1".repeat(32), revision: "b".repeat(64), status: "completed",
    state: "pass", score: null, type: "binary", direction: null, min: null, max: null };
  const right = { ...left, id: "2".repeat(32), revision: "c".repeat(64), state: "fail" };
  await resolveJson(requests[1], { evaluatorFingerprint: "a".repeat(64), dimension: "quality",
    pairKey: "pair_id", variantKey: "variant", leftVariant: "left", rightVariant: "right",
    candidateRows: 2, declaredPairs: 1, usablePairs: 1, technicalFailurePairs: 0, dimensionType: "binary",
    exclusions: {}, binary: { bothPass: 0, bothFail: 0, leftPassRightFail: 1,
      leftFailRightPass: 0 }, numeric: null, examples: [{ pairId: "case_1", left, right }] });
  await comparing;
  tree = render(ui.MatchedConversationCompare, hooks, props);
  assert.match(textOf(tree), /First passes, second fails:\s+1/);
  const opening = findAll(tree, (node) => node.type === "button" && textOf(node).includes("case_1"))[0].props.onClick();
  const detail = (side, variant) => ({ conversation: { id: side.id, revision: side.revision,
    labels: { pair_id: "case_1", variant }, messages: [{ role: "user", content: "Synthetic question." }] },
    assessments: [{ evaluator_fingerprint: "a".repeat(64), target_position: null,
      revision: side.revision, status: "completed",
      rubric: { target: "conversation", dimensions: [{ name: "quality", type: "binary" }] },
      dimensions: { quality: { state: side.state, score: null } } }] });
  await resolveJson(requests[2], detail(left, "left"));
  await resolveJson(requests[3], detail(right, "right"));
  await opening;
  tree = render(ui.MatchedConversationCompare, hooks, props);
  assert.match(textOf(tree), /Synthetic question/);
  const reopening = findAll(tree, (node) => node.type === "button" && textOf(node).includes("case_1"))[0].props.onClick();
  await resolveJson(requests[4], detail(left, "left"));
  await resolveJson(requests[5], { ...detail(right, "right"), conversation: {
    ...detail(right, "right").conversation, revision: "d".repeat(64) } });
  await reopening;
  tree = render(ui.MatchedConversationCompare, hooks, props);
  assert.match(textOf(tree), /changed.*Run the comparison again/);
  assert.doesNotMatch(textOf(tree), /Synthetic question/);
  const oldRequest = findAll(tree, (node) => node.type === "form")[0]
    .props.onSubmit({ preventDefault() {} });
  tree = render(ui.MatchedConversationCompare, hooks, props);
  findAll(tree, (node) => node.type === "input" && node.props["aria-label"] === "Second variant value")[0]
    .props.onChange({ target: { value: "new_variant" } });
  await resolveJson(requests[6], { candidateRows: 2, declaredPairs: 1, usablePairs: 1,
    exclusions: {}, examples: [], binary: { bothPass: 1 } });
  await oldRequest;
  tree = render(ui.MatchedConversationCompare, hooks, props);
  assert.doesNotMatch(textOf(tree), /Selected variant rows/);
  findAll(tree, (node) => node.type === "input" && node.props["aria-label"] === "Second variant value")[0]
    .props.onChange({ target: { value: "right" } });
  tree = render(ui.MatchedConversationCompare, hooks, props);
  const extremeRequest = findAll(tree, (node) => node.type === "form")[0]
    .props.onSubmit({ preventDefault() {} });
  await resolveJson(requests[7], { candidateRows: 2, declaredPairs: 1, usablePairs: 1,
    exclusions: {}, dimensionType: "number", numeric: { unavailableReason: "score_arithmetic_overflow" },
    binary: null, examples: [] });
  await extremeRequest;
  tree = render(ui.MatchedConversationCompare, hooks, props);
  assert.match(textOf(tree), /numeric range is too large for a finite aggregate/);
  const numericRequest = findAll(tree, (node) => node.type === "form")[0]
    .props.onSubmit({ preventDefault() {} });
  await resolveJson(requests[8], { candidateRows: 2, declaredPairs: 1, usablePairs: 1,
    exclusions: {}, dimensionType: "number", direction: "higher_is_better",
    numeric: { leftMean: 2, rightMean: 4, meanRightMinusLeft: 2,
      deltaInterval95: [-5, 5] }, binary: null, examples: [] });
  await numericRequest;
  tree = render(ui.MatchedConversationCompare, hooks, props);
  assert.match(textOf(tree), /Mean score on paired cases: first\s+2\s+, second\s+4/);
  assert.match(textOf(tree), /second minus first\s+2/);
});

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

test("recorded group selection reaches the filtered run API and can be cleared", async () => {
  const ui = await loadUiModule();
  for (const [field, value, query] of [
    ["sourceKind", "custom/agent", /source_kind=custom%2Fagent/],
    ["agentName", "research worker", /agent_name=research\+worker/],
  ]) {
    const hooks = createEffectHooks();
    const requests = deferredFetches();
    let cleared = 0;
    const props = { url: "/api/runs", [field]: value, onClearGroup: () => { cleared += 1; } };
    render(ui.Runs, hooks, props);
    hooks.flushEffects();
    assert.match(requests[0].url, query);
    const page = runListPage(0, ["run-a"], 1);
    page.selectedRun = runListRow("run-a");
    await resolveJson(requests[0], page);
    const tree = render(ui.Runs, hooks, props);
    assert.match(textOf(tree), /Recorded\s+(source|agent):/);
    assert.doesNotMatch(textOf(tree), /2026-09-01T00:00:00Z/);
    assert.match(textOf(tree), /Sep 1, 2026/);
    findAll(tree, (node) => node.type === "button" && textOf(node) === "Clear group")[0].props.onClick();
    assert.equal(cleared, 1);
  }
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  render(ui.Runs, hooks, { url: "/api/runs", sourceKind: "codex", agentName: "Research worker" });
  hooks.flushEffects();
  assert.match(requests[0].url, /source_kind=codex/);
  assert.match(requests[0].url, /agent_name=Research\+worker/);
});

test("an exact run investigation can browse other recorded runs by agent or source", async () => {
  const ui = await loadUiModule();
  const cases = [
    { agentName: "Research worker", expected: { agentName: "Research worker", sourceKind: "codex" }, label: /Browse codex runs by Research worker/ },
    { agentName: null, expected: { agentName: null, sourceKind: "codex" }, label: /Browse codex runs/ },
  ];
  for (const entry of cases) {
    const hooks = createEffectHooks();
    const requests = deferredFetches();
    const browsed = [];
    const run = runListRow("issue-run");
    run.agentName = entry.agentName;
    const props = {
      url: "/api/runs", focusRunIds: [run.runId], selectedRunId: run.runId,
      onBrowseRecordedGroup: (group) => browsed.push(group),
    };
    render(ui.Runs, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], {
      ...runListPage(0, [run.runId], 1), runs: [run], selectedRun: run,
    });
    const tree = render(ui.Runs, hooks, props);
    const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
    const rendered = render(detail.type, createHooks(), {
      ...detail.props, detail: { loading: false, data: {
        runId: run.runId, events: [], turns: [], children: [],
        page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false },
      } },
    });
    const browse = findAll(rendered, (node) => node.type === "button" && entry.label.test(textOf(node)))[0];
    assert.ok(browse);
    browse.props.onClick();
    assert.deepEqual(browsed, [entry.expected]);
  }
});

test("Agent Runs requests lightweight rows and expands only the selected run", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.match(requests[0].url, /view=list/);
  const page = runListPage(0, ["run-a", "run-b"], 2);
  page.runs = page.runs.map(({ findings, metrics, ...row }) => row);
  page.selectedRun = runListRow("run-a");
  await resolveJson(requests[0], page);
  let tree = render(ui.Runs, hooks, props);
  const generalBrowser = findAll(tree, (node) => node.type === "details"
    && node.props.className === "verdict-investigation-browser")[0];
  assert.equal(generalBrowser.props.open, true,
    "the ordinary Investigation destination must show its run list immediately");
  hooks.flushEffects();
  assert.match(textOf(tree), /run-a/);
  assert.doesNotMatch(textOf(tree), /0 observed errors/);
  const firstDetail = requests.find((request) => request.url.includes("/api/runs/run-a?"));
  assert.ok(firstDetail);
  await resolveJson(firstDetail, { runId: "run-a", turns: [{ turnId: "turn-a", request: "private-a" }],
    events: [], page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false } });
  render(ui.Runs, hooks, props);
  const other = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("run-b"))[0];
  other.props.onClick();
  tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const next = requests.find((request) => request.url.includes("selected_run_id=run-b"));
  assert.ok(next);
  assert.doesNotMatch(textOf(tree), /run-a.*source-reported tokens/s);
  await resolveJson(next, { ...page, selectedRun: runListRow("run-b") });
  tree = render(ui.Runs, hooks, props);
  const selectedDetail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  assert.equal(selectedDetail.props.run.runId, "run-b");
  assert.equal(selectedDetail.props.detail.data, null,
    "old run evidence must not render even before the next effect clears it");
  hooks.flushEffects();
  assert.match(textOf(tree), /run-b/);
});

test("multi-agent detail labels unavailable parents and unlinked neighbors without guessing handoffs", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const page = runListPage(0, ["parent", "child"], 2);
  page.runs[0].agentName = "Coordinator";
  page.runs[1].agentName = "Researcher";
  page.selectedRun = page.runs[0];
  await resolveJson(requests[0], page);
  const tree = render(ui.Runs, hooks, props);
  const component = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  assert.equal(component.props.otherAgentRunsVisible, true);
  const base = { ...component.props, detail: { loading: false, data: {
    runId: "parent", parentRunId: null, children: [], events: [], turns: [],
    page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false },
  } } };
  assert.match(textOf(render(component.type, createHooks(), base)), /Other captured agents on this page are unlinked/);
  const absent = render(component.type, createHooks(), { ...base, detail: { ...base.detail,
    data: { ...base.detail.data, parentRunId: "missing", parentAvailable: false } } });
  assert.match(textOf(absent), /parent run ID was recorded, but that run is unavailable in this tenant/);
  assert.doesNotMatch(textOf(absent), /Open recorded parent run/);
  const present = render(component.type, createHooks(), { ...base, detail: { ...base.detail,
    data: { ...base.detail.data, parentRunId: "parent", parentAvailable: true } } });
  assert.match(textOf(present), /Open recorded parent run/);
  const older = render(component.type, createHooks(), { ...base, detail: { ...base.detail,
    data: { ...base.detail.data, parentRunId: "parent" } } });
  assert.match(textOf(older), /Open recorded parent run/);
});

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

test("Agent Runs can fetch one exact Turn evaluator without reusing Trace coverage", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs", evaluatorFingerprint: "trace-only" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], runListPage(0, ["run-a"]));
  let tree = render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const initial = requests.find((request) => request.url.includes("/run-a?"));
  assert.ok(initial);
  assert.doesNotMatch(initial.url, /turn_evaluator_fingerprint/);
  await resolveJson(initial, { runId: "run-a", turns: [], turnPage: { available: 0, shown: 0, offset: 0, limit: 20, truncated: false }, events: [], page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false } });
  tree = render(ui.Runs, hooks, props);
  let detail = findAll(tree, (node) => typeof node.type === "function" && node.type.name === "RunDetail")[0];
  assert.ok(detail);
  assert.match(textOf(render(detail.type, createHooks(), detail.props)), /latest valid current Turn result among the 8 newest evaluator slots/);
  const digest = "a".repeat(64);
  let rendered = render(detail.type, createHooks(), detail.props);
  findAll(rendered, (node) => node.type === "input" && node.props["aria-label"] === "Exact Turn evaluator fingerprint")[0]
    .props.onChange({ target: { value: digest } });
  tree = render(ui.Runs, hooks, props);
  detail = findAll(tree, (node) => typeof node.type === "function" && node.type.name === "RunDetail")[0];
  rendered = render(detail.type, createHooks(), detail.props);
  findAll(rendered, (node) => node.type === "button" && textOf(node) === "Show exact evaluator")[0]
    .props.onClick();
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const exactA = requests.find((request) => request.url.includes(`turn_evaluator_fingerprint=${digest}`));
  assert.ok(exactA);
  const digestB = "b".repeat(64);
  tree = render(ui.Runs, hooks, props);
  detail = findAll(tree, (node) => typeof node.type === "function" && node.type.name === "RunDetail")[0];
  rendered = render(detail.type, createHooks(), detail.props);
  findAll(rendered, (node) => node.type === "input" && node.props["aria-label"] === "Exact Turn evaluator fingerprint")[0]
    .props.onChange({ target: { value: digestB } });
  tree = render(ui.Runs, hooks, props);
  detail = findAll(tree, (node) => typeof node.type === "function" && node.type.name === "RunDetail")[0];
  rendered = render(detail.type, createHooks(), detail.props);
  findAll(rendered, (node) => node.type === "button" && textOf(node) === "Show exact evaluator")[0]
    .props.onClick();
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const exactB = requests.find((request) => request.url.includes(`turn_evaluator_fingerprint=${digestB}`));
  assert.ok(exactB);
  const detailPage = (fingerprint) => ({ runId: "run-a", turns: [{ turnId: "turn-a", sequence: 0,
    status: "completed", evaluation: { evaluatorFingerprint: fingerprint,
      status: "completed", rubricName: "quality", rubricVersion: "1", judgeModels: ["test"],
      dimensions: [], toolEvidence: "counts_v1",
      toolCounts: { calls: 4, results: 4, errorResults: 0, unknownResults: 0,
        mcpCalls: 2, applicationCalls: 1, providerHostedCalls: 0,
        originNotCapturedCalls: 1, originUnusableCalls: 0 } } }],
    turnPage: { available: 1, shown: 1, offset: 0, limit: 20, truncated: false },
    events: [], page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false } });
  await resolveJson(exactB, detailPage(digestB));
  await resolveJson(exactA, detailPage(digest));
  tree = render(ui.Runs, hooks, props);
  detail = findAll(tree, (node) => typeof node.type === "function" && node.type.name === "RunDetail")[0];
  assert.equal(detail.props.detail.data.turns[0].evaluation.evaluatorFingerprint, digestB);
  assert.match(textOf(render(detail.type, createHooks(), detail.props)), /Recorded tool events:\s+4\s+calls/);
  const toolText = textOf(render(detail.type, createHooks(), detail.props));
  assert.match(toolText, /Direct dispatch origins:\s+2\s+MCP/);
  assert.match(toolText, /outer dispatch boundary/);
  assert.doesNotMatch(toolText, /MCP was not used/);
  const defaultDetail = detailPage(digestB);
  defaultDetail.turns[0].evaluation.toolEvidence = "none";
  const defaultText = textOf(render(detail.type, createHooks(), {
    ...detail.props, detail: { ...detail.props.detail, data: defaultDetail },
  }));
  assert.match(defaultText, /tool counts not included in this judgment/);
  assert.doesNotMatch(defaultText, /no tool evidence/);
});

test("captured run case drafting skips incomplete requests", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const drafts = [];
  const props = { url: "/api/runs", onDraftCase: (draft) => drafts.push(draft) };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], runListPage(0, ["run-a"]));
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], { runId: "run-a", turns: [
    { turnId: "partial", request: "partial text", requestTruncated: true },
    { turnId: "complete", request: "redacted complete task", requestTruncated: false },
  ], events: [], page: { available: 0, shown: 0 }, turnPage: { available: 2, shown: 2 } });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => typeof node.type === "function" && node.type.name === "RunDetail")[0];
  const rendered = render(detail.type, createHooks(), detail.props);
  findAll(rendered, (node) => node.type === "button" && textOf(node).includes("Draft a reviewed case"))[0].props.onClick();
  assert.deepEqual(drafts, [{ runId: "run-a", turnId: "complete", request: "redacted complete task", complete: true }]);
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

test("Agent Runs never renders a previous group's run before the new list request starts", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const first = { url: "/api/runs", sourceKind: "source-a", agentName: "worker-a" };
  const next = { url: "/api/runs", sourceKind: "source-b", agentName: "worker-b" };

  render(ui.Runs, hooks, first);
  hooks.flushEffects();
  await resolveJson(requests[0], runListPage(0, ["old-run"], 1));
  let tree = render(ui.Runs, hooks, first);
  hooks.flushEffects();
  assert.match(textOf(tree), /old-run/);

  tree = render(ui.Runs, hooks, next);
  assert.equal(findAll(tree, (node) => node.type?.name === "Notice")[0]?.props.text,
    "Loading agent runs…");
  assert.doesNotMatch(textOf(tree), /old-run/);
  hooks.flushEffects();
  assert.match(requests.at(-1).url, /source_kind=source-b/);
  assert.match(requests.at(-1).url, /agent_name=worker-b/);
  await resolveJson(requests.at(-1), runListPage(0, ["new-run", "other-run"], 2));
  tree = render(ui.Runs, hooks, next);
  assert.match(textOf(tree), /new-run/);
  assert.doesNotMatch(textOf(tree), /old-run/);

  findAll(tree, (node) => node.type === "button" && textOf(node).includes("other-run"))[0]
    .props.onClick();
  tree = render(ui.Runs, hooks, next);
  assert.match(textOf(tree), /new-run/);
  assert.match(textOf(tree), /other-run/);
  assert.equal(findAll(tree, (node) => node.type?.name === "RunDetail")[0].props.run.runId,
    "other-run");

  tree = render(ui.Runs, hooks, { url: "/api/runs" });
  assert.equal(findAll(tree, (node) => node.type?.name === "Notice")[0]?.props.text,
    "Loading agent runs…");
  assert.doesNotMatch(textOf(tree), /new-run|other-run/);
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

test("report period selection reloads the real API and survives refresh", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));

  dashboard.props.onReload(7);
  assert.match(requests[0].url, /report_days=7(?:&|$)/);
  await resolveJson(requests[0], bundle("evaluator-a", [], 7));

  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.data.managementReport.period.days, 7);
  dashboard.props.onReload();
  assert.match(requests[1].url, /report_days=7(?:&|$)/);
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

test("selected model call offers an exact evaluation action", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a", [{
    trace_id: "selected-call", provider: "openai", request_model: "model",
    prompt_redacted: "synthetic prompt", response_redacted: "synthetic response", hour: 1,
  }]);
  const selected = [];
  const tree = render(ui.Traces, createHooks(), {
    data, selectedTraceId: "selected-call", onEvaluateTrace: (id) => selected.push(id),
  });
  const detail = findAll(tree, (node) => node.type?.name === "TraceDetail")[0];
  assert.equal(detail.props.s.trace_id, "selected-call");
  const action = findAll(render(ui.TraceDetail, createHooks(), detail.props),
    (node) => node.type === "button" && textOf(node) === "Evaluate this model call")[0];
  action.props.onClick();
  assert.deepEqual(selected, ["selected-call"]);
});

test("Evaluator Lab sends the selected Trace and clears it when the unit changes", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const cleared = [];
  const props = { configUrl: "/api/config", selection: { unit: "trace", traceId: "selected-call" },
    onClearSelection: () => cleared.push(true) };
  render(ui.EvaluatorLab, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { providers: [{ provider: "anthropic", configured: true }] });
  let tree = render(ui.EvaluatorLab, hooks, props);
  assert.match(textOf(tree), /Selected\s+Trace selected-call/);
  const preview = findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Preview eligible calls"))[0].props.onClick();
  assert.equal(JSON.parse(requests[2].options.body).traceId, "selected-call");
  await resolveJson(requests[2], { unit: "trace", availableTraces: 1, eligible: 1,
    alreadyJudged: 0, notEvaluable: 0, notEvaluableReasons: {}, plannedCalls: 1,
    estimatedMaximumCostUsd: 0.01, maximumOutputTokens: 512,
    rubric: { dimensions: ["relevance"], skippedDimensions: [] },
    planFingerprint: "selected-plan", plannedTraces: [{ traceId: "selected-call" }] });
  await preview;
  tree = render(ui.EvaluatorLab, hooks, props);
  const unit = findAll(tree, (node) => node.type === "select" &&
    findAll(node, (child) => child.type === "option" && child.props.value === "agent_turn").length)[0];
  unit.props.onChange({ target: { value: "agent_turn" } });
  assert.deepEqual(cleared, [true]);
  tree = render(ui.EvaluatorLab, hooks, { ...props, selection: null });
  assert.doesNotMatch(textOf(tree), /Selected\s+Trace selected-call/);
  assert.equal(findAll(tree, (node) => node.type === "button" &&
    textOf(node).includes("Run 1 judge calls")).length, 0);
});

test("recorded span selection binds Explain to an owned span in the selected call", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { traceId: "selected-call", root: "" };
  render(ui.TraceSpans, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "token" });
  assert.match(requests[1].url, /\/api\/explain\/spans\/selected-call$/);
  assert.equal(requests[1].options.headers["X-Verdict-Setup"], "token");
  await resolveJson(requests[1], { spans: [{ spanId: "owned-span", name: "tool step",
    durationMs: 12, error: null }], truncated: false });
  let tree = render(ui.TraceSpans, hooks, props);
  const select = findAll(tree, (node) => node.type === "button"
    && textOf(node).includes("tool step"))[0];
  select.props.onClick();
  tree = render(ui.TraceSpans, hooks, props);
  const explain = findAll(tree, (node) => node.type?.name === "EvidenceExplain")[0];
  assert.equal(explain.props.scope, "span");
  assert.equal(explain.props.traceId, "selected-call");
  assert.equal(explain.props.spanId, "owned-span");
});

test("rubric AI draft adds a reviewed local criterion and invalidates evaluator preview", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const props = { configUrl: "/api/config", selection: { unit: "trace", traceId: "selected-call" } };
  let tree = render(ui.EvaluatorLab, hooks, props);
  const assist = findAll(tree, (node) => node.type?.name === "EvidenceExplain"
    && node.props.draftKind === "rubric")[0];
  assert.ok(assist);
  assert.equal(assist.props.traceId, "selected-call");
  assist.props.onApplyDraft({ name: "response_relevance",
    description: "The response addresses the selected request.", why: "Visible output." });
  tree = render(ui.EvaluatorLab, hooks, props);
  assert.ok(findAll(tree, (node) => node.type === "input"
    && node.props.value === "response_relevance").length);
  assert.ok(findAll(tree, (node) => node.type === "textarea"
    && node.props.value === "The response addresses the selected request.").length);
  assert.doesNotMatch(textOf(render(ui.EvaluatorLab, createHooks(),
    { configUrl: "/api/config" })), /Ask AI for a rubric draft/);
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

  assert.match(rendered, /Some dashboard views are bounded/);
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

  assert.match(rendered, /Connected store/);
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
  const data = bundle("evaluator-a");
  data.driftSignals = [
    { id: "signal-a", direction: "regression", provider: "anthropic" },
    { id: "signal-b", direction: "improvement", provider: "openai" },
  ];
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

test("date-filtered Trace Explorer labels its server-side count and clear action", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a", [{ trace_id: "day-call", provider: "openai",
    request_model: "test", started_at: "2026-10-07T23:00:00Z", hour: 0,
    prompt_redacted: "request", response_redacted: "response" }]);
  data.truncation.resources.traceSamples = { available: 1, shown: 1, limit: 30 };
  let cleared = 0;
  const tree = render(ui.Traces, createHooks(), { data, source: "live", traceDate: "2026-10-07",
    onClearDate: () => { cleared += 1; } });
  const rendered = textOf(tree).replace(/\s+/g, " ");
  assert.match(rendered, /Model calls on 2026-10-07 UTC/);
  assert.match(rendered, /Showing 1–1 of 1 model calls on this UTC date/);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Show all dates")[0]
    .props.onClick();
  assert.equal(cleared, 1);
});

test("a requested call date hides the previous snapshot through loading and failure", async () => {
  const ui = await loadUiModule();
  const oldData = bundle("judge-a", [{ trace_id: "old-date-call", provider: "openai",
    request_model: "test", started_at: "2026-10-06T10:00:00Z", hour: 0,
    prompt_redacted: "old date prompt", response_redacted: "old reply" }]);
  const newData = bundle("judge-a", [{ trace_id: "new-date-call", provider: "openai",
    request_model: "test", started_at: "2026-10-07T10:00:00Z", hour: 24,
    prompt_redacted: "new date prompt", response_redacted: "new reply" }]);
  const retried = [];
  globalThis.window = { location: { hash: "#tab=explore&section=calls&date=2026-10-07&judge=fail",
    pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    const hooks = createHooks();
    const props = { data: oldData, source: "live", reloading: true,
      loadedTraceFilter: { traceDate: "2026-10-06", traceJudgeStatus: "all", traceId: null },
      onTraceFilterChange: (...args) => retried.push(args) };
    let tree = render(ui.Dashboard, hooks, props);
    assert.match(textOf(tree), /Loading calls for the selected filter/);
    assert.equal(findAll(tree, (node) => node.type?.name === "Traces").length, 0);
    tree = render(ui.Dashboard, hooks, { ...props, reloading: false, loadError: "HTTP 503" });
    assert.match(textOf(tree), /Could not load calls for the selected filter/);
    assert.equal(findAll(tree, (node) => node.type?.name === "Traces").length, 0);
    findAll(tree, (node) => node.type === "button" && textOf(node) === "Retry loading calls")[0]
      .props.onClick();
    assert.deepEqual(retried, [["fail", null, "2026-10-07"]]);
    for (const loadedTraceFilter of [
      { traceDate: "2026-10-07", traceJudgeStatus: "all", traceId: null },
      { traceDate: "2026-10-07", traceJudgeStatus: "fail", traceId: "old-selection" },
    ]) {
      tree = render(ui.Dashboard, hooks, { ...props, loadedTraceFilter, reloading: true });
      assert.equal(findAll(tree, (node) => node.type?.name === "Traces").length, 0);
    }
    tree = render(ui.Dashboard, hooks, { ...props, data: newData, reloading: false,
      loadedTraceFilter: { traceDate: "2026-10-07", traceJudgeStatus: "fail", traceId: null } });
    const visible = findAll(tree, (node) => node.type?.name === "Traces");
    assert.equal(visible.length, 1);
    assert.deepEqual(visible[0].props.data.samples.map((sample) => sample.trace_id), ["new-date-call"]);
  } finally { delete globalThis.window; }
});

test("loaded call filter identity publishes with the response and stays old after failure", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  const initial = dashboard.props.onTraceFilterChange("all", null, null);
  await resolveJson(requests[0], bundle("judge-a"));
  await initial;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.loadedTraceFilter.traceDate, null);
  const selected = dashboard.props.onTraceFilterChange("all", null, "2026-10-07");
  assert.match(requests[1].url, /trace_date=2026-10-07/);
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.loadedTraceFilter.traceDate, null);
  requests[1].resolve({ ok: false, status: 503, json: async () => ({ error: "unavailable" }) });
  await selected;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.loadedTraceFilter.traceDate, null);
  assert.match(dashboard.props.loadError, /Could not load/);
  const retry = dashboard.props.onTraceFilterChange("all", null, "2026-10-07");
  await resolveJson(requests[2], bundle("judge-a"));
  await retry;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.loadedTraceFilter.traceDate, "2026-10-07");
  assert.equal(dashboard.props.loadError, null);
});

test("a missing store after live data clears old call evidence on the selected date", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  const previous = bundle("judge-a", [{ trace_id: "old-date-call", provider: "openai",
    request_model: "test", started_at: "2026-10-06T10:00:00Z", hour: 0,
    prompt_redacted: "old prompt", response_redacted: "old reply" }]);
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  const initial = dashboard.props.onTraceFilterChange("all", null, "2026-10-06");
  await resolveJson(requests[0], previous);
  await initial;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.source, "live");
  const missing = dashboard.props.onTraceFilterChange("all", null, "2026-10-07");
  await resolveStatus(requests[1], 503, { state: "store_not_created" });
  await missing;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.source, "new");
  assert.deepEqual(dashboard.props.data.samples, []);
  globalThis.window = { location: { hash: "#tab=explore&section=calls&date=2026-10-07",
    pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), dashboard.props);
    assert.match(textOf(tree), /No local store is available/);
    assert.equal(findAll(tree, (node) => node.type?.name === "Traces").length, 0);
  } finally { delete globalThis.window; }
  const resumed = dashboard.props.onTraceFilterChange("all", null, "2026-10-07");
  const current = bundle("judge-a", [{ trace_id: "new-date-call", provider: "openai",
    request_model: "test", started_at: "2026-10-07T10:00:00Z", hour: 0,
    prompt_redacted: "new prompt", response_redacted: "new reply" }]);
  await resolveJson(requests[2], current);
  await resumed;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.source, "live");
  assert.deepEqual(dashboard.props.data.samples.map((row) => row.trace_id), ["new-date-call"]);
});

test("selected call detail stays separate from the complete paginated row list", async () => {
  const ui = await loadUiModule();
  const selectedTrace = { trace_id: "older-00", provider: "openai",
    request_model: "test", started_at: "2026-10-07T10:00:00Z", hour: 0,
    prompt_redacted: "selected", response_redacted: "reply" };
  const page = bundle("judge-a", [{ trace_id: "older-30", provider: "openai",
    request_model: "test", started_at: "2026-10-07T10:00:00Z", hour: 0,
    prompt_redacted: "first page", response_redacted: "reply" }]);
  page.selectedTrace = selectedTrace;
  const tree = render(ui.Traces, createHooks(), { data: page, source: "live",
    selectedTraceId: "older-00", traceOffset: 0 });
  assert.deepEqual(findAll(tree, (node) => node.type?.name === "TraceDetail")
    .map((node) => node.props.s.trace_id), ["older-00"]);
  assert.equal(page.samples.length, 1);

  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  const secondPage = bundle("judge-a", [selectedTrace]);
  const initial = dashboard.props.onTracePageChange(30);
  await resolveJson(requests[0], secondPage);
  await initial;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.traceOffset, 30);
  const selected = dashboard.props.onTraceFilterChange("all", "older-00", null);
  assert.match(requests[1].url, /trace_offset=30/);
  await resolveJson(requests[1], secondPage);
  await selected;
  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.traceOffset, 30);
  assert.deepEqual(dashboard.props.data.samples.map((row) => row.trace_id), ["older-00"]);
});

test("opening a model-call chart date clears an earlier judge-only Calls filter", async () => {
  const ui = await loadUiModule();
  const routes = [];
  globalThis.window = { location: { hash: "#tab=explore&section=calls&judge=fail",
    pathname: "/dashboard", search: "" },
    history: { pushState(_state, _title, url) { routes.push(url); }, replaceState() {} },
    addEventListener() {}, removeEventListener() {} };
  try {
    const hooks = createHooks();
    const data = bundle("judge-a");
    let tree = render(ui.Dashboard, hooks, { data, source: "live" });
    findAll(tree, (node) => node.type === "button" && textOf(node).trim() === "Home")[0].props.onClick();
    tree = render(ui.Dashboard, hooks, { data, source: "live" });
    const home = findAll(tree, (node) => node.type?.name === "GuidedHome")[0];
    home.props.onNavigate("explore", "calls", "2026-10-07");
    assert.match(routes.at(-1), /date=2026-10-07/);
    assert.doesNotMatch(routes.at(-1), /judge=fail/);
  } finally { delete globalThis.window; }
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
  assert.match(rendered, /Provider error recorded/);
  assert.match(rendered, /No evaluation result/);
  assert.match(rendered, /Aug 23, 22:20 UTC/);
  assert.match(rendered, /Response was not captured for this trace/);
  assert.doesNotMatch(rendered, /response.*historical metadata-only trace/i);
});

test("trace detail identifies a tool-only response without calling it uncaptured", async () => {
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
  assert.match(rendered, /No provider error recorded/);
  assert.doesNotMatch(rendered, /Provider outcome.*succeeded/);
  assert.doesNotMatch(rendered, /Response was not captured for this trace/);
});

test("selected unfinished trace shows the same evidence state in header and facts", async () => {
  const ui = await loadUiModule();
  const sample = {
    trace_id: "unfinished", provider: "openai", request_model: "test",
    traceState: "unfinished", providerStatus: "no_error_recorded",
    prompt_redacted: "Synthetic prompt", response_redacted: null,
    started_at: "2026-08-23T22:20:00Z", error: null,
    deterministicFacts: { providerOutcome: "no_error_recorded" },
  };
  const tree = render(ui.TraceDetail, createHooks(), { s: sample, onClose() {} });
  const rendered = textOf(tree);
  assert.match(rendered, /Unfinished/);
  assert.equal(findAll(tree, (node) => node.type?.name === "TraceFact"
    && node.props?.label === "Provider outcome")[0].props.value,
  "Unfinished; no provider error recorded");
  assert.doesNotMatch(rendered, /Content partially captured No provider error recorded/);
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
  assert.equal(factValues["Provider outcome"], "No provider error recorded");
  assert.equal(factValues["Judge evidence"], "Eligible");
  assert.equal(factValues["Valid JSON"], "Yes");
  assert.equal(factValues["Refusal signature"], "Not detected");
  assert.equal(factValues.Tokens, "10 in / 4 out");
  assert.match(rendered, /do not determine correctness, sentiment, or response quality/);
});

test("trace detail uses singular relative time for one minute, hour, and day", async () => {
  const ui = await loadUiModule();
  const now = Date.UTC(2026, 9, 20, 1, 0, 0);
  const previous = Date.now;
  Date.now = () => now;
  try {
    for (const [elapsed, expected] of [
      [60_000, "1 minute ago"], [3_600_000, "1 hour ago"], [86_400_000, "1 day ago"],
    ]) {
      const rendered = textOf(render(ui.TraceDetail, createHooks(), {
        s: { trace_id: `relative-${elapsed}`, started_at: new Date(now - elapsed).toISOString() },
        onClose() {},
      }));
      assert.match(rendered, new RegExp(expected));
      assert.doesNotMatch(rendered, /1 (?:minutes|hours|days) ago/);
    }
  } finally {
    Date.now = previous;
  }
});

test("judge scores directs an empty store to Evaluator Lab without legacy windows", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");

  const rendered = textOf(render(ui.Judge, createHooks(), { data })).replace(/\s+/g, " ");

  assert.match(rendered, /No Trace evaluator results have been stored yet/);
  assert.match(rendered, /Evaluator Lab stores results for an eligible trace set/);
  assert.doesNotMatch(rendered, /global content-bearing traces/);
});

async function resolveStatus(request, status, payload) {
  request.resolve({ ok: false, status, json: async () => payload });
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

test("a store that setup has not created yet is first run, not a load failure", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  dashboard.props.onReload();
  await resolveStatus(requests[0], 503, { error: "data unavailable", state: "store_not_created" });

  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.equal(dashboard.props.loadError, null);
  assert.equal(dashboard.props.source, "new");
});

test("a failed first load does not claim that a snapshot is still displayed", async () => {
  const ui = await loadUiModule();
  const hooks = createHooks();
  const requests = deferredFetches();
  let dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  dashboard.props.onReload();
  await resolveStatus(requests[0], 503, { error: "data unavailable" });

  dashboard = dashboardElement(render(ui.DashboardRoot, hooks));
  assert.doesNotMatch(dashboard.props.loadError, /Still showing|evaluator/);
  assert.match(dashboard.props.loadError, /Could not load dashboard data/);
});

async function untilRequests(requests, count, timer) {
  for (let index = 0; index < 500 && requests.length < count; index += 1) {
    await new Promise((resolve) => timer(resolve, 1));
  }
  assert.ok(requests.length >= count, `expected ${count} requests, saw ${requests.length}`);
}

test("local capture starts a server job and shows its progress until it completes", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const completed = [];
  const props = { configUrl: "/api/config", onComplete: (source) => completed.push(source), agentSummary: {} };
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback) => realSetTimeout(callback, 0);
  try {
    render(ui.SetupWizard, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], { setupToken: "setup-token" });
    assert.match(requests[1].url, /\/api\/setup\/capture\/status$/);
    await resolveJson(requests[1], { job: null });
    const button = (label) => findAll(render(ui.SetupWizard, hooks, props),
      (node) => node.type === "button" && textOf(node) === label)[0];
    const status = () => findAll(render(ui.SetupWizard, hooks, props),
      (node) => node.props?.role === "status").map(textOf).join(" ");

    const previewing = button("Preview sources").props.onClick();
    assert.match(status(), /Previewing sources/);
    await resolveJson(requests[2], { claude: { files: 1 }, codex: { files: 0 } });
    await previewing;

    const capturing = button("Approve and capture").props.onClick();
    assert.match(requests[3].url, /\/api\/setup\/capture$/);
    assert.equal(requests[3].options.method, "POST");
    await resolveJson(requests[3], { job: { jobId: "job-1", state: "running", filesDone: 0, filesTotal: null } });
    assert.match(status(), /Listing history files/);
    assert.equal(button("Approve and capture").props.disabled, true);
    await untilRequests(requests, 5, realSetTimeout);
    assert.match(requests[4].url, /\/api\/setup\/capture\/status\?job=job-1$/);
    await resolveJson(requests[4], { job: { jobId: "job-1", state: "running", filesDone: 12, filesTotal: 120 } });
    assert.match(status(), /Importing 12 of 120 history files/);
    await untilRequests(requests, 6, realSetTimeout);
    await resolveJson(requests[5], { job: { jobId: "job-1", state: "analyzing", filesDone: 120, filesTotal: 120 } });
    assert.match(status(), /Analyzing captured evidence/);
    await untilRequests(requests, 7, realSetTimeout);
    await resolveJson(requests[6], { job: { jobId: "job-1", state: "completed", filesDone: 120, filesTotal: 120,
      summary: { stored: 120, event_limited_runs: 1 }, analysis: { status: "completed" } } });
    await capturing;
    assert.equal(status(), "");
    assert.deepEqual(completed, ["local"]);
    assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
    assert.match(textOf(render(ui.SetupWizard, hooks, props)), /Selected recorded failures.*monitored issues/);
    assert.match(textOf(render(ui.SetupWizard, hooks, props)), /linked model calls.*call-level evaluation/);
    assert.doesNotMatch(textOf(render(ui.SetupWizard, hooks, props)), /does not enter monitoring or evaluation/);
    const connected = { ...props, agentSummary: { totalAgentRuns: 120 } };
    render(ui.SetupWizard, hooks, connected);
    hooks.flushEffects();
    assert.match(textOf(render(ui.SetupWizard, hooks, connected)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
    findAll(render(ui.SetupWizard, hooks, connected),
      (node) => node.type === "button" && textOf(node) === "Manage data sources")[0].props.onClick();
    findAll(render(ui.SetupWizard, hooks, connected),
      (node) => node.type === "button" && textOf(node) === "Existing telemetry file")[0].props.onClick();
    assert.match(textOf(render(ui.SetupWizard, hooks, connected)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});

test("a completed local capture warning survives leaving and returning to Data Sources", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: {
    jobId: "last-capture", state: "completed", finishedAt: "2026-10-19T21:00:00Z",
    summary: { stored: 1, source_partial_files: 1,
      partial_reasons: { bounded_source_prefix: 1 },
      event_limited_runs: 1, recent_sample_errors: 1 }, analysis: { status: "completed" },
  } });
  const visible = textOf(render(ui.SetupWizard, hooks, props));
  assert.match(visible, /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
  assert.match(visible, /could not refresh the recent event sample for\s+1\s+session/);
  assert.match(visible, /1\s+history file.*partial source coverage.*bounded source prefix/is);
});

test("a failed recapture does not relabel the prior limited capture as the latest job", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: {
    jobId: "last-good", state: "completed",
    summary: { stored: 1, event_limited_runs: 1 }, analysis: { status: "completed" },
  } });
  const button = (label) => findAll(render(ui.SetupWizard, hooks, props),
    (node) => node.type === "button" && textOf(node) === label)[0];
  button("Manage data sources").props.onClick();
  const preview = button("Preview sources").props.onClick();
  await resolveJson(requests[2], { codex: { files: 1 } });
  await preview;
  const capture = button("Approve and capture").props.onClick();
  await resolveJson(requests[3], { job: { jobId: "failed", state: "failed", error: "capture failed" } });
  await capture;
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session/is);
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /capture failed/);
});

test("failed setup capture refreshes captured evidence and shows skipped files", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const completed = [];
  const refreshed = [];
  const props = { configUrl: "/api/config", agentSummary: {},
    onComplete: (source) => completed.push(source), onRefresh: () => refreshed.push(true) };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: null });
  const button = (label) => findAll(render(ui.SetupWizard, hooks, props),
    (node) => node.type === "button" && textOf(node) === label)[0];
  const preview = button("Preview sources").props.onClick();
  await resolveJson(requests[2], { codex: { files: 2 } });
  await preview;
  const capture = button("Approve and capture").props.onClick();
  await resolveJson(requests[3], { job: { jobId: "partial", state: "failed",
    error: "capture_incomplete", summary: { files: 2, stored: 1, skipped: 1,
      skip_reasons: { malformed_jsonl: 1 } } } });
  await capture;
  const visible = textOf(render(ui.SetupWizard, hooks, props));
  assert.deepEqual(completed, []);
  assert.deepEqual(refreshed, [true]);
  assert.match(visible, /1\s+skipped/i);
  assert.doesNotMatch(visible, /Import finished/);
});

test("a prior completed capture warning returns after a failed rescan and route remount", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: {
    jobId: "failed", state: "failed", error: "capture_failed",
  }, lastImportedSummary: { stored: 1, event_limited_runs: 1 } });
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session/is);
});

test("a remounted Data Sources view explains durable failed scan attempts", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: null, attempts: {
    trackedFiles: 2, failedFiles: 1, completeFiles: 1, partialFiles: 0,
    unfinishedFiles: 0, failureReasons: { malformed_jsonl: 1 },
  } });
  const visible = textOf(render(ui.SetupWizard, hooks, props));
  assert.match(visible, /Latest recorded scan attempts failed for\s+1\s+history file/);
  assert.match(visible, /malformed jsonl \(1\)/);
  assert.doesNotMatch(visible, /PRIVATE_PATH_CANARY/);
});

test("a remounted Data Sources view warns about interrupted and cancelled scan attempts", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: null, attempts: {
    trackedFiles: 3, failedFiles: 0, completeFiles: 1, partialFiles: 0,
    unfinishedFiles: 1, cancelledFiles: 1, failureReasons: {},
  } });
  const visible = textOf(render(ui.SetupWizard, hooks, props));
  assert.match(visible, /1\s+interrupted.*1\s+cancelled.*rescan/is);
});

test("a remounted Data Sources view preserves the bounded-source warning", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: null, attempts: {
    trackedFiles: 1, completeFiles: 0, partialFiles: 1, failedFiles: 0,
    unfinishedFiles: 0, cancelledFiles: 0, sourcePartialFiles: 1,
    partialReasons: { bounded_source_prefix: 1 }, failureReasons: {},
  } });
  assert.match(textOf(render(ui.SetupWizard, hooks, props)),
    /1\s+history file.*partial source coverage.*bounded source prefix/is);
});

test("a stored limited import remains visible when analysis fails after capture", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: {
    jobId: "analysis-failed", state: "failed", error: "analysis_failed",
    summary: { stored: 1, event_limited_runs: 1 },
  }, lastImportedSummary: { stored: 1 } });
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
});

test("a limited import warning survives a later pre-summary failure and remount", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 2 } };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: {
    jobId: "pre-summary-failed", state: "failed", error: "capture_failed", summary: null,
  }, lastImportedSummary: { stored: 1, event_limited_runs: 1 } });
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
});

test("an analysis failure after a mounted import still shows its capture limit", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const completed = [];
  const props = { configUrl: "/api/config", agentSummary: { totalAgentRuns: 1 },
    onComplete: (source) => completed.push(source) };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  await resolveJson(requests[1], { job: null });
  const button = (label) => findAll(render(ui.SetupWizard, hooks, props),
    (node) => node.type === "button" && textOf(node) === label)[0];
  button("Manage data sources").props.onClick();
  const preview = button("Preview sources").props.onClick();
  await resolveJson(requests[2], { codex: { files: 1 } });
  await preview;
  const capture = button("Approve and capture").props.onClick();
  await resolveJson(requests[3], { job: { jobId: "failed-analysis", state: "failed",
    error: "analysis_failed", summary: { stored: 1, event_limited_runs: 1 } } });
  await capture;
  assert.deepEqual(completed, []);
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
});

test("an old capture-status response cannot erase a newer limited capture", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", agentSummary: {}, onComplete: () => {} };
  render(ui.SetupWizard, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { setupToken: "setup-token" });
  const button = (label) => findAll(render(ui.SetupWizard, hooks, props),
    (node) => node.type === "button" && textOf(node) === label)[0];
  const preview = button("Preview sources").props.onClick();
  await resolveJson(requests[2], { codex: { files: 1 } });
  await preview;
  const capture = button("Approve and capture").props.onClick();
  await resolveJson(requests[3], { job: {
    jobId: "new", state: "completed", finishedAt: "2026-10-19T22:00:00Z",
    summary: { stored: 1, event_limited_runs: 1 }, analysis: { status: "completed" },
  } });
  await capture;
  await resolveJson(requests[1], { job: {
    jobId: "old", state: "completed", finishedAt: "2026-10-19T21:00:00Z",
    summary: { stored: 1 }, analysis: { status: "completed" },
  } });
  assert.match(textOf(render(ui.SetupWizard, hooks, props)), /A recent local import reported\s+1\s+session\s+beyond the bounded run view/);
});

test("guided home offers retry after an evidence request fails", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", source: "live",
    meta: { totalAgentRuns: 2, totalTraces: 0, monitorEligibleCalls: 0 }, monitor: null };
  let tree = render(ui.GuidedHome, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { documents: [], openIssues: 0, worker: { status: "unverified" } });
  tree = render(ui.GuidedHome, hooks, props);
  findAll(tree, (node) => node.type?.name === "Insights")[0].props.onData(null, "HTTP 503");
  tree = render(ui.GuidedHome, hooks, props);
  assert.match(textOf(tree), /Some evidence could not be loaded/);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Retry loading"))[0].props.onClick();
  render(ui.GuidedHome, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], { documents: [], openIssues: 0, worker: { status: "unverified" } });
  tree = render(ui.GuidedHome, hooks, props);
  findAll(tree, (node) => node.type?.name === "Insights")[0].props.onData({
    analysisState: { status: "completed" }, findings: [],
  }, null);
  assert.match(textOf(render(ui.GuidedHome, hooks, props)), /Review suggested monitoring/);
});

test("guided home keeps historical findings available while leading to reviewed setup", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const destinations = [];
  const opened = [];
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights", usageUrl: "/api/usage",
    source: "live", meta: { totalAgentRuns: 8, totalTraces: 0, monitorEligibleCalls: 0 },
    monitor: null, onNavigate: (...destination) => destinations.push(destination),
    onOpenFinding: (finding) => opened.push(finding.code) };
  render(ui.GuidedHome, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { documents: [], openIssues: 0, worker: { status: "unverified" } });
  let tree = render(ui.GuidedHome, hooks, props);
  const finding = { code: "tool_error", severity: "error", runs: 124, runIds: ["run-1"] };
  findAll(tree, (node) => node.type?.name === "Insights")[0].props.onData({
    analysisState: { status: "completed" }, findings: [finding,
      { code: "event_capture_partial", severity: "warning", runs: 3, runIds: ["run-2"] }],
  }, null);
  tree = render(ui.GuidedHome, hooks, props);
  assert.match(textOf(tree), /Review suggested monitoring/);
  assert.doesNotMatch(textOf(tree), /Inspect a recorded issue|124 affected runs/);
  const metrics = findAll(tree, (node) => node.type === "section"
    && node.props.className === "verdict-home-metric");
  assert.match(textOf(metrics[2]), /Recorded issue types 1 Current analyzed scope/);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Review suggested setup"))[0]
    .props.onClick();
  assert.deepEqual(destinations, [["monitor", "plan"]]);
  findAll(tree, (node) => node.type?.name === "Insights")[0].props.onOpenRuns(finding);
  assert.deepEqual(opened, ["tool_error"]);
  findAll(tree, (node) => node.type?.name === "Insights")[0].props.onData({
    analysisState: { status: "completed" },
    findings: [{ code: "event_capture_partial", severity: "warning", runs: 3, runIds: ["run-2"] }],
  }, null);
  tree = render(ui.GuidedHome, hooks, props);
  const gapOnlyMetrics = findAll(tree, (node) => node.type === "section"
    && node.props.className === "verdict-home-metric");
  assert.match(textOf(gapOnlyMetrics[2]), /Evidence gaps 1 Current analyzed scope/);
  assert.doesNotMatch(textOf(gapOnlyMetrics[2]), /Recorded issue types 0/);
  assert.match(textOf(tree), /Inspect evidence gaps/);
  assert.doesNotMatch(textOf(tree), /Review suggested monitoring/);
  const previousDocument = globalThis.document;
  const focusCalls = [];
  globalThis.document = { getElementById(id) {
    focusCalls.push(`target:${id}`);
    return { scrollIntoView() { focusCalls.push("scroll"); },
      focus() { focusCalls.push("focus"); } };
  } };
  try {
    findAll(tree, (node) => node.type === "button" && textOf(node).includes("Inspect evidence gaps"))[0]
      .props.onClick();
  } finally { globalThis.document = previousDocument; }
  assert.deepEqual(focusCalls, ["target:guided-evidence-gaps", "scroll", "focus"]);
});

test("evidence-gap investigation does not call partial capture a failure", async () => {
  const ui = await loadUiModule();
  const originalWindow = globalThis.window;
  try {
    for (const [finding, expected, excluded] of [
      ["event_capture_partial", /Inspect an evidence gap.*what was and was not recorded/s, /Review the recorded failure/],
      ["tool_outcomes_incomplete", /Inspect an evidence gap.*what was and was not recorded/s, /capture limit|Review the recorded failure/],
      ["test_failed", /A captured run recorded a failed test.*Review the recorded finding/s, /Inspect an evidence gap/],
      ["possible_tool_loop", /Repeated tool calls need review.*Review the recorded finding/s, /Review the recorded failure/],
    ]) {
      globalThis.window = { location: { hash: `#tab=explore&section=runs&finding=${finding}&run=run-a&selected=run-a`,
        pathname: "/dashboard", search: "" },
      history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
      const page = textOf(render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" }));
      assert.match(page, expected);
      assert.doesNotMatch(page, excluded);
    }
  } finally { globalThis.window = originalWindow; }
});

test("issue inbox ignores an older open response after switching to reviewed history", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  let tree = render(ui.IssueInbox, hooks, props);
  hooks.flushEffects();
  assert.match(requests[0].url, /state=open/);
  assert.match(textOf(tree), /Loading monitoring status/);
  assert.doesNotMatch(textOf(tree), /NO RECURRING PLAN/);
  await resolveJson(requests[0], { issues: [{ noticeId: "open", runId: "open-run",
    eventId: "event", checkCode: "test_failed", eventAt: "now", state: "open" }],
    nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  tree = render(ui.IssueInbox, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Refresh")[0].props.onClick();
  findAll(tree, (node) => node.type === "button" && textOf(node) === "View reviewed issue history")[0]
    .props.onClick();
  tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Loading monitoring status/);
  assert.doesNotMatch(textOf(tree), /open-run/);
  hooks.flushEffects();
  assert.match(requests[6].url, /state=reviewed/);
  await resolveJson(requests[6], { issues: [{ noticeId: "reviewed", runId: "reviewed-run",
    eventId: "event", checkCode: "test_failed", eventAt: "now", state: "acknowledged" }],
    nextOffset: null, state: "reviewed" });
  await resolveJson(requests[7], { setupToken: "token" });
  await resolveJson(requests[8], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], { issues: [{ noticeId: "open", runId: "stale-open-run",
    eventId: "event", checkCode: "test_failed", eventAt: "now", state: "open" }],
    nextOffset: null, state: "open" });
  await resolveJson(requests[4], { setupToken: "token" });
  await resolveJson(requests[5], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Reviewed issue history/);
  assert.match(textOf(tree), /reviewed-run/);
  assert.doesNotMatch(textOf(tree), /open-run/);
});

test("dashboard snapshot refresh also reloads Monitor readiness and notices", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: { totalAgentRuns: 2 }, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [{ kind: "schedule",
    documentId: "daily", state: "active", revision: 4 }],
  worker: { status: "recent" }, lastCycle: { state: "completed", trigger: "manual" } });
  let tree = render(ui.IssueInbox, hooks, props);
  assert.doesNotMatch(textOf(tree), /Latest worker check failed/);

  // The parent receives a new /api/data snapshot with unchanged evidence counts.
  render(ui.IssueInbox, hooks, { ...props, meta: { totalAgentRuns: 2 } });
  hooks.flushEffects();
  assert.equal(requests.length, 6);
  await resolveJson(requests[3], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[4], { setupToken: "token" });
  await resolveJson(requests[5], { openIssues: 0, documents: [{ kind: "schedule",
    documentId: "daily", state: "active", revision: 4 }],
  worker: { status: "unverified" }, lastCycle: { state: "failed", trigger: "worker",
    scheduleRevision: 4, failureCode: "delivery_failed" } });
  tree = render(ui.IssueInbox, hooks, { ...props, meta: { totalAgentRuns: 2 } });
  assert.match(textOf(tree), /Latest\s+worker\s+check failed:\s+delivery failed/);
  assert.match(textOf(tree), /worker not verified/i);
});

test("successful review with failed refresh shows error and hides stale action", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "open", runId: "run-1",
    eventId: "event-1", checkCode: "test_failed", eventAt: "now", state: "open" }],
    nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  const tree = render(ui.IssueInbox, hooks, props);
  const review = findAll(tree, (node) => node.type === "button" && textOf(node) === "Acknowledge")[0]
    .props.onClick();
  await resolveJson(requests[3], { state: "acknowledged" });
  requests[4].resolve({ ok: false, status: 503, json: async () => ({}) });
  await resolveJson(requests[5], { setupToken: "token" });
  await resolveJson(requests[6], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await review;
  const after = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(after), /Could not refresh monitoring status/);
  assert.doesNotMatch(textOf(after), /Acknowledge/);
  assert.doesNotMatch(textOf(after), /run-1/);
});

test("issue inbox shows linked later pass without calling it recovery", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { configUrl: "/api/config", meta: {}, monitor: null,
    onOpenEvidence: (value) => opened.push(value) };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "open", runId: "failed-run",
    eventId: "failed-event", checkCode: "test_failed", eventAt: "2026-10-01T00:00:00Z",
    state: "open", rechecks: [{ runId: "passed-run", eventId: "passed-event",
      eventAt: "2026-10-02T00:00:00Z" }] }], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  const tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Compare the failed event with the attached later pass/);
  assert.doesNotMatch(textOf(tree), /Rerun the relevant test/);
  const component = findAll(tree, (node) => typeof node.type === "function"
    && node.type.name === "RecheckEvidence")[0];
  const recheck = render(component.type, createEffectHooks(), component.props);
  assert.match(textOf(recheck), /Later recorded pass attached/);
  assert.match(textOf(recheck), /same work and overall recovery remain unverified/i);
  const linked = render(component.type, createEffectHooks(), {
    ...component.props,
    issue: { ...component.props.issue, rechecks: [{ runId: "passed-run",
      eventId: "passed-event", eventAt: "2026-10-02T00:00:00Z",
      sameCheckId: true }] },
  });
  assert.match(textOf(linked), /matching application-supplied check ID/i);
  assert.match(textOf(linked), /recovery remains unverified/i);
  const link = findAll(recheck, (node) => node.type === "button" &&
    textOf(node) === "Open recheck evidence →")[0];
  link.props.onClick();
  assert.deepEqual(opened.at(-1), { runId: "passed-run", eventId: "passed-event", noticeId: "open" });
});

test("issue inbox searches bounded later check candidates without auto-attaching", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { configUrl: "/api/config", meta: {}, monitor: null,
    onOpenEvidence: (value) => opened.push(value) };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "notice", runId: "failed-run",
    eventId: "failed-event", checkCode: "test_failed", eventAt: "2026-10-01T00:00:00Z",
    state: "open", rechecks: [] }], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  const tree = render(ui.IssueInbox, hooks, props);
  const component = findAll(tree, (node) => typeof node.type === "function"
    && node.type.name === "RecheckEvidence")[0];
  const detailHooks = createEffectHooks();
  let detail = render(component.type, detailHooks, component.props);
  const search = findAll(detail, (node) => node.type === "button" &&
    textOf(node) === "Find later matching checks")[0];
  const finding = search.props.onClick();
  assert.match(requests[3].url, /candidate-rechecks$/);
  await resolveJson(requests[3], { searchable: true, scanned: 200, partial: true,
    candidates: [{ runId: "later-run", eventId: "later-event", eventAt: "2026-10-02T00:00:00Z",
      recentRevision: "b".repeat(64), recentTurnId: "later-turn" }] });
  await finding;
  detail = render(component.type, detailHooks, component.props);
  assert.match(textOf(detail), /first\s+200\s+later event records/);
  assert.match(textOf(detail), /more may exist/i);
  assert.doesNotMatch(textOf(detail), /recovery verified/i);
  findAll(detail, (node) => node.type === "button" && textOf(node) === "Inspect candidate →")[0]
    .props.onClick();
  assert.deepEqual(opened, [{ runId: "later-run", eventId: "later-event",
    recentRevision: "b".repeat(64), recentTurnId: "later-turn" }]);
  findAll(detail, (node) => node.type === "button" && textOf(node) === "Use these IDs")[0]
    .props.onClick();
  detail = render(component.type, detailHooks, component.props);
  const inputs = findAll(detail, (node) => node.type === "input");
  assert.deepEqual(inputs.map((node) => node.props.value), ["later-run", "later-event"]);
  assert.equal(requests.length, 4);
});

test("multiple attached passes ask which results cover the failed work", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "open", runId: "failed-run",
    eventId: "failed-event", checkCode: "test_failed", eventAt: "2026-10-01T00:00:00Z",
    state: "open", rechecks: [
      { runId: "pass-one", eventId: "event-one", eventAt: "2026-10-02T00:00:00Z", sameCheckId: true },
      { runId: "pass-two", eventId: "event-two", eventAt: "2026-10-03T00:00:00Z", sameCheckId: false },
    ] }], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  const text = textOf(render(ui.IssueInbox, hooks, props));
  assert.match(text, /Compare the failed event with the attached later passes/);
  assert.match(text, /verify which, if any, cover the same work/);
  assert.doesNotMatch(text, /the attached later pass and verify/);
});

test("terminal run notice opens its run without an event recheck form", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { configUrl: "/api/config", meta: {}, monitor: null,
    onOpenEvidence: (value) => opened.push(value) };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "r".repeat(64),
    runId: "failed-run", eventId: null, evidenceKind: "run",
    checkCode: "run_failed", eventAt: "2026-10-01T00:00:00Z",
    state: "open", rechecks: [] }, { noticeId: "t".repeat(64),
    runId: "timed-out-run", eventId: null, evidenceKind: "run",
    checkCode: "run_timed_out", eventAt: "2026-09-30T00:00:00Z",
    state: "open", rechecks: [] }], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 2, documents: [],
    worker: { status: "unverified" } });
  const tree = render(ui.IssueInbox, hooks, props);
  const content = textOf(tree);
  assert.match(content, /An agent run ended failed/);
  assert.match(content, /An agent run timed out/);
  assert.match(content, /More recorded issues/);
  assert.match(content, /Inspect the recorded run status/);
  assert.doesNotMatch(content, /event null|More recorded events|this event alone/);
  assert.equal(findAll(tree, (node) => node.type?.name === "RecheckEvidence").length, 0);
  findAll(tree, (node) => node.type === "button" &&
    textOf(node) === "Open run evidence →")[0].props.onClick();
  assert.equal(opened[0].runId, "failed-run");
  assert.equal(opened[0].eventId, null);
});

test("issue recheck form posts exact event IDs and refreshes the inbox", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "a".repeat(64),
    runId: "failed-run", eventId: "failed-event", checkCode: "test_failed",
    eventAt: "2026-10-01T00:00:00Z", state: "open", rechecks: [] }],
    nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  const inbox = render(ui.IssueInbox, hooks, props);
  const component = findAll(inbox, (node) => typeof node.type === "function"
    && node.type.name === "RecheckEvidence")[0];
  const recheckHooks = createEffectHooks();
  let tree = render(component.type, recheckHooks, component.props);
  const inputs = findAll(tree, (node) => node.type === "input");
  inputs[0].props.onChange({ target: { value: "later-run" } });
  inputs[1].props.onChange({ target: { value: "later-event" } });
  tree = render(component.type, recheckHooks, component.props);
  const form = findAll(tree, (node) => node.type === "form")[0];
  const submission = form.props.onSubmit({ preventDefault() {} });
  assert.match(requests[3].url, /\/api\/control\/issues\/[a-f0-9]{64}\/rechecks$/);
  assert.equal(requests[3].options.headers["X-Verdict-Setup"], "token");
  assert.deepEqual(JSON.parse(requests[3].options.body),
    { runId: "later-run", eventId: "later-event" });
  await resolveJson(requests[3], { recheck: { runId: "later-run",
    eventId: "later-event", eventAt: "2026-10-02T00:00:00Z" } });
  await resolveJson(requests[4], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[5], { setupToken: "token" });
  await resolveJson(requests[6], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  await submission;
});

test("lead recheck form changes identity when review advances to another issue", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  const item = (id) => ({ noticeId: id.repeat(64), runId: `run-${id}`,
    eventId: `event-${id}`, checkCode: "test_failed", eventAt: "2026-10-01T00:00:00Z",
    state: "open", rechecks: [] });
  await resolveJson(requests[0], { issues: [item("a"), item("b")],
    nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 2, documents: [], worker: { status: "unverified" } });
  let tree = render(ui.IssueInbox, hooks, props);
  const form = findAll(tree, (node) => node.type?.name === "RecheckEvidence")[0];
  assert.equal(form.props.key, "a".repeat(64));
  const review = findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Acknowledge")[0].props.onClick();
  await resolveJson(requests[3], { state: "acknowledged" });
  await resolveJson(requests[4], { issues: [item("b")], nextOffset: null, state: "open" });
  await resolveJson(requests[5], { setupToken: "token" });
  await resolveJson(requests[6], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  await review;
  tree = render(ui.IssueInbox, hooks, props);
  const next = findAll(tree, (node) => node.type?.name === "RecheckEvidence")[0];
  assert.equal(next.props.key, "b".repeat(64));
  assert.notEqual(next.props.key, form.props.key);
});

test("manual Monitoring refresh failure hides the former no-issues and worker status", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  let tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Recurring issue notices: none/);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Refresh")[0].props.onClick();
  tree = render(ui.IssueInbox, hooks, props);
  assert.doesNotMatch(textOf(tree), /Recurring issue notices: none/);
  requests[3].resolve({ ok: false, status: 503, json: async () => ({}) });
  await resolveJson(requests[4], { setupToken: "token" });
  await resolveJson(requests[5], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Could not refresh monitoring status/);
  assert.doesNotMatch(textOf(tree), /Recurring issue notices: none/);
});

test("monitoring page identifies a paused plan instead of claiming no plan", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0,
    documents: [{ kind: "schedule", state: "disabled" }],
    worker: { status: "recent" }, lastCycle: { state: "completed", trigger: "worker" } });
  const tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /PLAN PAUSED/);
  assert.doesNotMatch(textOf(tree), /NO RECURRING PLAN/);
});

test("monitoring page leads to saved linked findings without calling them recurring notices", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 6, totalTraces: 0 }, monitor: null,
    onOpenFinding: (finding) => opened.push(finding.code) };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  assert.match(requests[3].url, /\/api\/insights/);
  await resolveJson(requests[3], { analysisState: { status: "completed",
    cutoff: "2026-10-17T09:59:00Z", completedAt: "2026-10-17T10:00:00Z" },
    scope: { availableRuns: 6, analyzedRuns: 6, complete: true, traces: { available: 0 } },
    findings: [{ code: "tool_error", severity: "error", runs: 1, runIds: ["r1"] }] });
  const tree = render(ui.IssueInbox, hooks, props);
  const page = textOf(tree);
  assert.match(page, /Saved analysis found/);
  assert.match(page, /1 affected run across/);
  assert.match(page, /Analysis completed:/);
  assert.match(page, /Tool failures recorded/);
  assert.match(page, /Recurring issue notices: none/);
  assert.match(page, /RECORDED ISSUE TO REVIEW/);
  assert.doesNotMatch(page, /No open recorded failures/);
  assert.equal(findAll(tree, (node) => node.type === "button" && textOf(node).includes("Inspect affected runs")).length, 1);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Inspect affected runs"))[0]
    .props.onClick();
  assert.deepEqual(opened, ["tool_error"]);
});

test("active recurring plan still leads with a current linked finding when no notice or alert exists", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 6, totalTraces: 0 }, monitor: null,
    onOpenFinding: (finding) => opened.push(finding.code) };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0,
    documents: [{ kind: "schedule", documentId: "daily", state: "active", revision: 2 }],
    worker: { status: "recent" },
    lastCycle: { state: "completed", trigger: "worker", scheduleRevision: 2 } });
  await resolveJson(requests[3], { analysisState: { status: "completed",
    cutoff: "2026-10-17T09:59:00Z", completedAt: "2026-10-17T10:00:00Z" },
    scope: { availableRuns: 6, analyzedRuns: 6, complete: true, traces: { available: 0 } },
    findings: [{ code: "tool_error", severity: "error", runs: 2, runIds: ["r1", "r2"] }] });
  const tree = render(ui.IssueInbox, hooks, props);
  const page = textOf(tree);
  assert.match(page, /RECORDED ISSUE TO REVIEW/);
  assert.ok(page.indexOf("RECORDED ISSUE TO REVIEW") < page.search(/Latest completed\s+worker\s+check/));
  assert.match(page, /Tool failures recorded/);
  assert.match(page, /Next local check/);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Inspect affected runs"))[0]
    .props.onClick();
  assert.deepEqual(opened, ["tool_error"]);
});

test("Monitoring refreshes stale saved analysis locally and only shows updated findings after reread", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 6, totalTraces: 0 }, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], { analysisState: { status: "completed" },
    scope: { availableRuns: 5, analyzedRuns: 5, complete: true, traces: { available: 0 } },
    findings: [{ code: "tool_error", severity: "error", runs: 1, runIds: ["r1"] }] });
  let tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Saved findings need a fresh local check/);
  assert.doesNotMatch(textOf(tree), /Inspect affected runs/);
  const refresh = findAll(tree, (node) => node.type === "button" && textOf(node) === "Refresh local analysis")[0]
    .props.onClick();
  assert.match(requests[4].url, /\/api\/insights\/run/);
  assert.equal(requests[4].options.method, "POST");
  tree = render(ui.IssueInbox, hooks, props);
  assert.doesNotMatch(textOf(tree), /Inspect affected runs/);
  await resolveJson(requests[4], { analysisState: { status: "completed" } });
  await resolveJson(requests[5], { analysisState: { status: "completed" },
    scope: { availableRuns: 6, analyzedRuns: 6, complete: true, traces: { available: 0 } },
    findings: [{ code: "test_failed", severity: "error", runs: 1, runIds: ["r2"] }] });
  await refresh;
  tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Test failures recorded/);
  assert.doesNotMatch(textOf(tree), /Captured activity changed/);
});

test("global refresh during local analysis does not suppress its completed reread", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  let parentReloads = 0;
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 2, totalTraces: 0 }, monitor: null,
    onRefreshStore: () => { parentReloads += 1; } };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  const old = { analysisState: { status: "completed" }, scope: { availableRuns: 1,
    analyzedRuns: 1, complete: true, traces: { available: 0 } }, findings: [] };
  await resolveJson(requests[3], old);
  let tree = render(ui.IssueInbox, hooks, props);
  const pendingRun = findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Refresh local analysis")[0].props.onClick();
  assert.match(requests[4].url, /\/api\/insights\/run/);
  const refreshed = { ...props, meta: { totalAgentRuns: 2, totalTraces: 0 } };
  render(ui.IssueInbox, hooks, refreshed); hooks.flushEffects();
  await resolveJson(requests[5], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[6], { setupToken: "token" });
  await resolveJson(requests[7], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[8], old);
  await resolveJson(requests[4], { analysisState: { status: "completed" } });
  assert.match(requests[9].url, /\/api\/insights$/);
  await resolveJson(requests[9], { analysisState: { status: "completed" },
    scope: { availableRuns: 2, analyzedRuns: 2, complete: true,
      traces: { available: 0 } },
    findings: [{ code: "test_failed", severity: "error", runs: 1, runIds: ["r2"] }] });
  await pendingRun;
  tree = render(ui.IssueInbox, hooks, refreshed);
  assert.match(textOf(tree), /Test failures recorded/);
  assert.equal(parentReloads, 2); // mount refresh and post-analysis refresh
});

test("old-source analysis completion cannot refresh or block a newly selected source", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  let parentReloads = 0;
  const old = { configUrl: "/a/api/config", insightsUrl: "/a/api/insights",
    meta: { totalAgentRuns: 2 }, monitor: null,
    onRefreshStore: () => { parentReloads += 1; } };
  const stale = { analysisState: { status: "completed" },
    scope: { availableRuns: 1, analyzedRuns: 1, complete: true,
      traces: { available: 0 } }, findings: [] };
  render(ui.IssueInbox, hooks, old); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], stale);
  let tree = render(ui.IssueInbox, hooks, old);
  const pendingRun = findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Refresh local analysis")[0].props.onClick();
  const next = { ...old, configUrl: "/b/api/config", insightsUrl: "/b/api/insights",
    meta: { totalAgentRuns: 2 } };
  render(ui.IssueInbox, hooks, next); hooks.flushEffects();
  await resolveJson(requests[5], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[6], { setupToken: "token" });
  await resolveJson(requests[7], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[8], stale);
  await resolveJson(requests[4], { analysisState: { status: "completed" } });
  await pendingRun;
  assert.equal(requests.length, 9);
  assert.equal(parentReloads, 2);
  tree = render(ui.IssueInbox, hooks, next);
  assert.equal(findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Refresh local analysis")[0].props.disabled, false);
});

test("source switch during post-analysis reread does not reload the old parent", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  let parentReloads = 0;
  const old = { configUrl: "/a/api/config", insightsUrl: "/a/api/insights",
    meta: { totalAgentRuns: 2 }, monitor: null,
    onRefreshStore: () => { parentReloads += 1; } };
  const stale = { analysisState: { status: "completed" },
    scope: { availableRuns: 1, analyzedRuns: 1, complete: true,
      traces: { available: 0 } }, findings: [] };
  render(ui.IssueInbox, hooks, old); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], stale);
  const tree = render(ui.IssueInbox, hooks, old);
  const pendingRun = findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Refresh local analysis")[0].props.onClick();
  await resolveJson(requests[4], { analysisState: { status: "completed" } });
  assert.match(requests[5].url, /\/a\/api\/insights$/);
  const next = { ...old, configUrl: "/b/api/config", insightsUrl: "/b/api/insights",
    meta: { totalAgentRuns: 2 } };
  render(ui.IssueInbox, hooks, next); hooks.flushEffects();
  await resolveJson(requests[6], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[7], { setupToken: "token" });
  await resolveJson(requests[8], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[9], stale);
  await resolveJson(requests[5], { analysisState: { status: "completed" },
    scope: { availableRuns: 2, analyzedRuns: 2, traces: { available: 0 } }, findings: [] });
  await pendingRun;
  assert.equal(parentReloads, 2);
});

test("review completion reloads the filter selected while its POST was pending", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "issue-1", runId: "run-1",
    eventId: "event-1", checkCode: "test_failed", eventAt: "2026-10-17T10:00:00Z",
    state: "open" }], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  let tree = render(ui.IssueInbox, hooks, props);
  const pendingReview = findAll(tree, (node) => node.type === "button"
    && textOf(node) === "Acknowledge")[0].props.onClick();
  findAll(tree, (node) => node.type === "button"
    && textOf(node) === "View reviewed issue history")[0].props.onClick();
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  assert.match(requests[4].url, /state=reviewed/);
  await resolveJson(requests[4], { issues: [], nextOffset: null, state: "reviewed" });
  await resolveJson(requests[5], { setupToken: "token" });
  await resolveJson(requests[6], { openIssues: 1, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], { state: "acknowledged" });
  assert.match(requests[7].url, /state=reviewed/);
  await resolveJson(requests[7], { issues: [{ noticeId: "issue-1", runId: "run-1",
    eventId: "event-1", checkCode: "test_failed", eventAt: "2026-10-17T10:00:00Z",
    state: "acknowledged" }], nextOffset: null, state: "reviewed" });
  await resolveJson(requests[8], { setupToken: "token" });
  await resolveJson(requests[9], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await pendingReview;
  tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Reviewed issue history/);
  assert.doesNotMatch(textOf(tree), /Loading monitoring status/);
});

test("failed local analysis refresh does not promote old findings", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 6, totalTraces: 0 }, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], { analysisState: { status: "completed" },
    scope: { availableRuns: 5, analyzedRuns: 5, complete: true, traces: { available: 0 } },
    findings: [{ code: "tool_error", severity: "error", runs: 1, runIds: ["r1"] }] });
  const before = render(ui.IssueInbox, hooks, props);
  const refresh = findAll(before, (node) => node.type === "button" && textOf(node) === "Refresh local analysis")[0]
    .props.onClick();
  requests[4].resolve({ ok: false, status: 503, json: async () => ({}) });
  await refresh;
  const after = textOf(render(ui.IssueInbox, hooks, props));
  assert.match(after, /No updated finding is assumed/);
  assert.doesNotMatch(after, /Inspect affected runs/);
});

test("Monitoring names a persisted analysis failure and offers a retry", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", insightsUrl: "/api/insights",
    meta: { totalAgentRuns: 6, totalTraces: 0 }, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, documents: [], worker: { status: "unverified" } });
  await resolveJson(requests[3], { analysisState: { status: "error" }, findings: [], scope: {} });
  const tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Latest local analysis failed/);
  assert.match(textOf(tree), /Retry local analysis/);
  assert.doesNotMatch(textOf(tree), /No completed saved analysis is available/);
});

test("monitoring page leads with issue action and explains prospective call watch", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { configUrl: "/api/config", meta: { totalAgentRuns: 8,
    monitorEligibleCalls: 40 }, monitor: { active: { snapshot: {
      manifest: { prospective_open: true, current_unit_ids: ["call-1"] },
      comparison: { status: "insufficient", metrics: [] },
    }, policy: { prospective_target: 30 } } },
  onOpenEvidence: (issue) => opened.push(issue.eventId),
  onOpenHistory: () => opened.push("history"),
  onOpenUsage: () => opened.push("usage"),
  onOpenDeliveries: () => opened.push("deliveries"),
  onOpenGroups: () => opened.push("groups") };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [{ noticeId: "issue-1", runId: "run-1",
    eventId: "event-1", checkCode: "test_failed", eventAt: "2026-10-17T10:00:00Z", state: "open" }],
    nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 1,
    deliveryQueue: { failed: 1, blocked: 1, pending: 7 },
    documents: [{ kind: "schedule", documentId: "daily", state: "active", revision: 2 }],
    worker: { status: "recent" }, nextDue: { scheduleRevision: 2,
      nextDueAt: "2026-10-19T10:00:00Z" },
    lastCycle: { state: "completed", trigger: "worker", scheduleRevision: 2 } });
  const tree = render(ui.IssueInbox, hooks, props);
  const text = textOf(tree);
  assert.match(text, /MODEL-CALL MONITOR COLLECTING/);
  assert.match(text, /RECORDED FAILURE TO INSPECT/);
  assert.ok(text.indexOf("RECORDED FAILURE TO INSPECT") < text.indexOf("Open recurring issues"));
  assert.ok(text.indexOf("RECORDED FAILURE TO INSPECT") < text.indexOf("Other watch areas"));
  assert.match(text, /A test failed/);
  assert.match(text, /2 alert deliveries need attention/);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Review alert deliveries"))[0].props.onClick();
  assert.ok(opened.includes("deliveries"));
  assert.match(text, /Rerun the relevant test and inspect its later result/);
  assert.match(text, /Collecting 1 of 30 eligible calls/);
  assert.match(text, /Inspect reported token totals and coverage for captured sources/);
  assert.doesNotMatch(text, /Follow reported tokens by day into the sessions and turns that used them/);
  assert.doesNotMatch(text, /no alert/i);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("Open run evidence"))[0]
    .props.onClick();
  assert.ok(opened.includes("event-1"));
  for (const [label, value] of [["Inspect token coverage", "usage"],
    ["Inspect model-call comparison", "history"], ["Review groups", "groups"]]) {
    findAll(tree, (node) => node.type === "button" && textOf(node).includes(label))[0].props.onClick();
    assert.ok(opened.includes(value));
  }
});

test("Monitoring token coverage card does not promise sessions in a call-only store", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = {
    configUrl: "/api/config", meta: { totalAgentRuns: 0, monitorEligibleCalls: 2 }, monitor: null,
  };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, deliveryQueue: { pending: 2, unbound: 1 },
    documents: [], worker: { status: "unverified" } });
  const tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /Inspect reported token totals and coverage for captured sources/);
  assert.doesNotMatch(textOf(tree), /sessions lack them/);
  assert.doesNotMatch(textOf(tree), /alert deliveries need attention/);
});

test("Monitoring names one blocked alert delivery without counting waiting items", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { issues: [], nextOffset: null, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 0, deliveryQueue: { blocked: 1, pending: 8 },
    documents: [], worker: { status: "unverified" } });
  assert.match(textOf(render(ui.IssueInbox, hooks, props)), /1 alert delivery needs attention/);
});

test("reviewing an open issue refreshes the first page before further paging", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { configUrl: "/api/config", meta: {}, monitor: null };
  render(ui.IssueInbox, hooks, props);
  hooks.flushEffects();
  const issue = (index) => ({ noticeId: `id-${index}`, runId: `run-${index}`,
    eventId: `event-${index}`, checkCode: "test_failed", eventAt: "now", state: "open" });
  await resolveJson(requests[0], { issues: Array.from({ length: 50 }, (_, i) => issue(i)),
    nextOffset: 50, state: "open" });
  await resolveJson(requests[1], { setupToken: "token" });
  await resolveJson(requests[2], { openIssues: 51, documents: [], worker: { status: "unverified" } });
  let tree = render(ui.IssueInbox, hooks, props);
  const reviewing = findAll(tree, (node) => node.type === "button" && textOf(node) === "Acknowledge")[0]
    .props.onClick();
  await resolveJson(requests[3], { state: "acknowledged" });
  assert.match(requests[4].url, /offset=0&state=open/);
  await resolveJson(requests[4], { issues: Array.from({ length: 50 }, (_, i) => issue(i + 1)),
    nextOffset: null, state: "open" });
  await resolveJson(requests[5], { setupToken: "token" });
  await resolveJson(requests[6], { openIssues: 50, documents: [], worker: { status: "unverified" } });
  await reviewing;
  tree = render(ui.IssueInbox, hooks, props);
  assert.match(textOf(tree), /run-50/);
  assert.equal(findAll(tree, (node) => node.type === "button" && textOf(node) === "Load more issues").length, 0);
});

test("Turn analysis loads only on click, links evidence, and ignores stale responses", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const focused = [];
  const props = { url: "/api/runs", runId: "run-a", turnId: "turn-a",
    onFocusEvent(id) { focused.push(id); } };
  let tree = render(ui.TurnAnalysis, hooks, props);
  hooks.flushEffects();
  assert.equal(requests.length, 0);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Analyze turn")[0].props.onClick();
  tree = render(ui.TurnAnalysis, hooks, props);
  assert.equal(requests[0].url, "/api/runs/run-a/turns/turn-a/analysis");
  assert.match(textOf(tree), /Analyzing captured events/);
  await resolveJson(requests[0], { runId: "run-a", turnId: "turn-a",
    eventCoverage: { shown: 2, available: 2, complete: true },
    actions: [{ eventId: "action", label: "Called read_file", resultEventId: "result" }],
    actionsTruncated: false,
    reviewSignals: [{ eventId: "failure", label: "Test execution reported failure" }],
    signalsTruncated: false });
  tree = render(ui.TurnAnalysis, hooks, props);
  assert.match(textOf(tree), /cannot establish the agent’s private intent/);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Called read_file")[0].props.onClick();
  findAll(tree, (node) => node.type === "button" && textOf(node) === "open matched result")[0].props.onClick();
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Test execution reported failure")[0].props.onClick();
  assert.deepEqual(focused, ["action", "result", "failure"]);

  findAll(tree, (node) => node.type === "button" && textOf(node) === "Refresh analysis")[0].props.onClick();
  tree = render(ui.TurnAnalysis, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Refresh analysis")[0].props.onClick();
  await resolveJson(requests[2], { runId: "run-a", turnId: "turn-a",
    eventCoverage: { shown: 0, available: 0, complete: true },
    actions: [], actionsTruncated: false, reviewSignals: [], signalsTruncated: false });
  await resolveJson(requests[1], { runId: "run-a", turnId: "turn-a",
    eventCoverage: { shown: 1, available: 1, complete: true },
    actions: [{ eventId: "stale", label: "Stale action" }], actionsTruncated: false,
    reviewSignals: [], signalsTruncated: false });
  tree = render(ui.TurnAnalysis, hooks, props);
  assert.match(textOf(tree), /No tool, command, delegation, or retry action/);
  assert.doesNotMatch(textOf(tree), /Stale action/);
});

test("Turn analysis warns when an earlier source limit omitted this turn's events", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs", runId: "run", turnId: "later" };
  let tree = render(ui.TurnAnalysis, hooks, props);
  hooks.flushEffects();
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Analyze turn")[0].props.onClick();
  await resolveJson(requests[0], {
    runId: "run", turnId: "later", requestState: "not_captured", responseState: "not_captured",
    eventCoverage: { shown: 0, available: 0, complete: false, sourcePartial: true,
      multipleProducers: false },
    actions: [], actionsTruncated: false, reviewSignals: [], signalsTruncated: false,
  });
  tree = render(ui.TurnAnalysis, hooks, props);
  assert.match(textOf(tree), /Source capture reported missing events/);
  assert.match(textOf(tree), /Later actions or outcomes may be missing/);
  assert.match(textOf(tree), /No failure signal in the events shown/);
});

test("Turn analysis keeps a hidden panel hidden after a late response and shows errors", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs", runId: "run", turnId: "turn" };
  let tree = render(ui.TurnAnalysis, hooks, props);
  hooks.flushEffects();
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Analyze turn")[0].props.onClick();
  tree = render(ui.TurnAnalysis, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Hide analysis")[0].props.onClick();
  await resolveJson(requests[0], { eventCoverage: { shown: 0, available: 0, complete: true },
    actions: [], reviewSignals: [], actionsTruncated: false, signalsTruncated: false });
  tree = render(ui.TurnAnalysis, hooks, props);
  assert.doesNotMatch(textOf(tree), /No failure signal/);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Analyze turn")[0].props.onClick();
  tree = render(ui.TurnAnalysis, hooks, props);
  findAll(tree, (node) => node.type === "button" && textOf(node) === "Refresh analysis")[0].props.onClick();
  requests[1].resolve({ ok: false, status: 503 });
  await new Promise(setImmediate);
  tree = render(ui.TurnAnalysis, hooks, props);
  assert.match(textOf(tree), /Turn analysis unavailable:\s+Error: HTTP 503/);
});

test("Agent Runs keeps same-run Turn detail while focusing evidence and clears it on run switch", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], runListPage(0, ["run-a", "run-b"], 2));
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], { runId: "run-a", turns: [{ turnId: "turn-a" }],
    events: [], page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false } });
  let tree = render(ui.Runs, hooks, props);
  let detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  detail.props.onFocusEvent("event-a");
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  tree = render(ui.Runs, hooks, props);
  detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  assert.equal(detail.props.detail.data.runId, "run-a");
  assert.equal(detail.props.detail.loading, true);
  findAll(tree, (node) => node.type === "button" && textOf(node).includes("run-b"))[0].props.onClick();
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  tree = render(ui.Runs, hooks, props);
  detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  assert.equal(detail.props.detail.data, null);
});

test("a linked issue keeps its verified later pass beside failed run evidence", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const noticeId = "n".repeat(64);
  const props = { url: "/api/runs", focusRunIds: ["failed-run"],
    selectedRunId: "failed-run", noticeId,
    onOpenNoticeEvidence: (runId, eventId, recentRevision, recentTurnId) =>
      opened.push([runId, eventId, recentRevision || null, recentTurnId || null]) };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.ok(requests.some((request) => request.url === `/api/control/issues/${noticeId}`));
  await resolveJson(requests[0], runListPage(0, ["failed-run"], 1));
  const noticeRequest = requests.find((request) => request.url === `/api/control/issues/${noticeId}`);
  await resolveJson(noticeRequest, { noticeId, runId: "failed-run", eventId: "failed-event",
    checkCode: "test_failed", eventAt: "2026-10-01T00:00:00Z", state: "open",
    recentRevision: "f".repeat(64), recentTurnId: "late-turn",
    rechecks: [{ runId: "later-run", eventId: "later-event", eventAt: "2026-10-02T00:00:00Z",
      sameCheckId: true }] });
  const tree = render(ui.Runs, hooks, props);
  const component = findAll(tree, (node) => node.type?.name === "NoticeContext")[0];
  assert.ok(component);
  const context = render(component.type, createHooks(), component.props);
  assert.match(textOf(context), /A later pass does not establish overall recovery/);
  const buttons = findAll(context, (node) => node.type === "button");
  buttons[0].props.onClick();
  buttons[1].props.onClick();
  assert.deepEqual(opened, [
    ["failed-run", "failed-event", "f".repeat(64), "late-turn"],
    ["later-run", "later-event", null, null],
  ]);
});

test("large local runs expose a separately labeled recent inspection sample", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = { ...runListRow("large-run"), sourceKind: "codex", eventCount: 1500 };
  const props = { url: "/api/runs", focusRunIds: ["large-run"], selectedRunId: "large-run" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["large-run"], 1), runs: [run] });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  const renderedDetail = render(detail.type, createHooks(), detail.props);
  const component = findAll(renderedDetail, (node) => node.type?.name === "RecentLocalEvents")[0];
  assert.ok(component);
  const sampleHooks = createEffectHooks();
  let sample = render(component.type, sampleHooks, component.props);
  sampleHooks.flushEffects();
  sample = render(component.type, sampleHooks, component.props);
  assert.match(textOf(sample), /Inspect the recent sample to determine what was retained/);
  assert.doesNotMatch(textOf(sample), /does not enter recurring monitoring or LLM evaluation/);
  const load = findAll(sample, (node) => node.type === "button" && textOf(node) === "View recent sample")[0];
  const pending = load.props.onClick();
  const request = requests.find((item) => item.url === "/api/runs/large-run/recent-local-events");
  assert.ok(request);
  await resolveJson(request, { runId: "large-run", capturedAt: "2026-10-19T00:00:00Z",
    sourceEventCount: 1600, sourceRecordCount: 1610,
    sourceRevision: "f".repeat(64), omittedMiddleEvents: 51,
    events: [
      { sourceOrdinal: 1596, eventId: "recent-success", occurredAt: "2026-10-19T00:01:00Z",
        type: "tool_result", status: "completed", provenance: "codex:tool",
        privacy: "metadata", attributes: { exit_code: 0 } },
      { sourceOrdinal: 1597, eventId: "recent-failure", occurredAt: "2026-10-19T00:02:00Z",
        type: "tool_result", status: "failed", provenance: "codex:tool",
        privacy: "metadata", attributes: { is_error: true } },
      { sourceOrdinal: 1598, eventId: "recent-unknown", occurredAt: "2026-10-19T00:03:00Z",
        type: "tool_result", status: "unknown", provenance: "codex:tool",
        privacy: "metadata", attributes: {} },
      { sourceOrdinal: 1599, eventId: "recent-timeout", occurredAt: "2026-10-19T00:04:00Z",
        type: "command", status: "timed_out", provenance: "codex:tool",
        privacy: "metadata", attributes: {} },
    ] });
  await pending;
  sample = render(component.type, sampleHooks, component.props);
  assert.match(textOf(sample), /legacy recent sample is for inspection only/);
  assert.match(textOf(sample), /Source event #\s*1600/);
  assert.match(textOf(sample), /51 source events outside the canonical prefix and this sample/);
  assert.match(textOf(sample), /2 of the 4 sampled events report failed or timed-out status/);
  assert.equal(findAll(sample, (node) => node.type === "details").length, 4);
  findAll(sample, (node) => node.type === "button" && textOf(node) === "Show reported failures")[0]
    .props.onClick();
  sample = render(component.type, sampleHooks, component.props);
  assert.equal(findAll(sample, (node) => node.type === "details").length, 2);
  assert.doesNotMatch(textOf(sample), /Source event #\s*1597/);
  assert.match(textOf(sample), /Source event #\s*1598/);
  assert.match(textOf(sample), /Source event #\s*1600/);
  findAll(sample, (node) => node.type === "button" && textOf(node) === "Show all sampled events")[0]
    .props.onClick();
  sample = render(component.type, sampleHooks, component.props);
  assert.equal(findAll(sample, (node) => node.type === "details").length, 4);
  const reloading = findAll(sample, (node) => node.type === "button" &&
    textOf(node) === "View recent sample")[0].props.onClick();
  const reloadRequest = requests.filter((item) => item.url === "/api/runs/large-run/recent-local-events").at(-1);
  await resolveJson(reloadRequest, { runId: "large-run", capturedAt: "2026-10-19T00:05:00Z",
    sourceEventCount: 1601, sourceRecordCount: 1611, sourceRevision: "a".repeat(64),
    omittedMiddleEvents: 52,
    events: [{ sourceOrdinal: 1600, eventId: "recent-unknown", occurredAt: "2026-10-19T00:05:00Z",
      type: "tool_result", status: "unknown", provenance: "codex:tool",
      privacy: "metadata", attributes: {} }] });
  await reloading;
  sample = render(component.type, sampleHooks, component.props);
  assert.match(textOf(sample), /No sampled events report failed or timed-out status/);
  assert.equal(findAll(sample, (node) => node.type === "details").length, 1);
  assert.equal(findAll(sample, (node) => node.type === "button" &&
    textOf(node) === "Show reported failures").length, 0);
  const switched = render(component.type, sampleHooks, { ...component.props, runId: "other-run" });
  assert.doesNotMatch(textOf(switched), /Source event #\s*1601|revision\s+aaaaaaaaaaaa/,
    "a new run must not briefly show the previous run's sampled evidence before effects flush");
});

test("a short recorded prefix with an oversized source exposes newest inspection", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = { ...runListRow("partial-run"), sourceKind: "codex", eventCount: 6,
    sourcePartial: true };
  const props = { url: "/api/runs", focusRunIds: ["partial-run"],
    selectedRunId: "partial-run" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["partial-run"], 1), runs: [run] });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  const renderedDetail = render(detail.type, createHooks(), detail.props);
  const component = findAll(renderedDetail,
    (node) => node.type?.name === "RecentLocalEvents")[0];
  assert.ok(component);
  const sampleHooks = createEffectHooks();
  render(component.type, sampleHooks, component.props);
  sampleHooks.flushEffects();
  const initial = render(component.type, sampleHooks, component.props);
  const pending = findAll(initial, (node) => node.type === "button" &&
    textOf(node) === "View recent sample")[0].props.onClick();
  const request = requests.find((item) => item.url ===
    "/api/runs/partial-run/recent-local-events");
  await resolveJson(request, { schema: "local-recent-window-v2", runId: "partial-run",
    capturedAt: "2026-10-19T00:00:00Z", sourceRevision: "f".repeat(64),
    sourceRecordCount: 8, sourceEventCount: 6, events: [], turns: [], findings: [],
    latestSample: { status: "sampled", records: [{ at: "2026-10-19T01:00:00Z",
      kind: "agent_message", preview: "Newest synthetic activity" }] } });
  await pending;
  const loaded = render(component.type, sampleHooks, component.props);
  assert.match(textOf(loaded), /Newest sampled source records/);
  assert.match(textOf(loaded), /Newest synthetic activity/);
  assert.match(textOf(loaded), /may overlap the canonical prefix/);
  assert.match(textOf(loaded), /cannot establish full-session totals/);
  const unavailablePending = findAll(loaded, (node) => node.type === "button" &&
    textOf(node) === "View recent sample")[0].props.onClick();
  const unavailableRequest = requests.filter((item) => item.url ===
    "/api/runs/partial-run/recent-local-events").at(-1);
  await resolveJson(unavailableRequest, { schema: "local-recent-window-v2", runId: "partial-run",
    capturedAt: "2026-10-19T00:00:00Z", sourceRevision: "f".repeat(64),
    sourceRecordCount: 8, sourceEventCount: 6, omittedMiddleEvents: 57,
    events: [], turns: [], findings: [],
    latestSample: { status: "unavailable_decoded_limit", records: [] } });
  await unavailablePending;
  const unavailable = textOf(render(component.type, sampleHooks, component.props));
  assert.match(unavailable, /newest source records are unavailable/);
  assert.doesNotMatch(unavailable, /newest source-record sample are available|inspect the latest available records/);
  assert.doesNotMatch(unavailable, /outside the canonical prefix and this sample|Browse recorded events below/);
});

test("admitted local history pages through recorded middle events", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = { ...runListRow("full-run"), sourceKind: "codex", eventCount: 1500 };
  const props = { url: "/api/runs", focusRunIds: ["full-run"], selectedRunId: "full-run" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["full-run"], 1), runs: [run] });
  const detail = findAll(render(ui.Runs, hooks, props),
    (node) => node.type?.name === "RunDetail")[0];
  const component = findAll(render(detail.type, createHooks(), detail.props),
    (node) => node.type?.name === "RecentLocalEvents")[0];
  const sampleHooks = createEffectHooks();
  render(component.type, sampleHooks, component.props);
  sampleHooks.flushEffects();
  const button = (label) => findAll(render(component.type, sampleHooks, component.props),
    (node) => node.type === "button" && textOf(node) === label)[0];
  const sampleLoad = button("View recent sample").props.onClick();
  await resolveJson(requests.find((item) => item.url.endsWith("/recent-local-events")), {
    schema: "local-recent-window-v2", durableFacts: true, runId: "full-run",
    sourceRevision: "a".repeat(64), sourceEventCount: 1600,
    sourceRecordCount: 1610, canonicalPrefixEvents: 1499,
    omittedMiddleEvents: 51, events: [], turns: [], findings: [],
  });
  await sampleLoad;
  const pageLoad = button("Browse recorded events").props.onClick();
  const pageRequest = requests.find((item) => item.url.includes("/local-events?"));
  assert.ok(pageRequest);
  await resolveJson(pageRequest, { coverage: "complete_recorded",
    sourceRevision: "a".repeat(64), sourceEventCount: 1600,
    events: [{ sourceOrdinal: 1499, eventId: "middle-1", turnId: "turn-1",
      type: "tool_result", status: "failed", occurredAt: "2026-10-20T00:00:00Z",
      attributes: { is_error: true }, firstSeenRevision: "a".repeat(64) }],
    nextCursor: { sourceOrdinal: 1499, eventId: "middle-1" },
  });
  await pageLoad;
  const browsedPage = render(component.type, sampleHooks, component.props);
  assert.match(textOf(browsedPage), /Source event #\s*1500/);
  assert.equal(findAll(browsedPage, (node) => node.type === "details"
    && /Source event #\s*1500/.test(textOf(node)))[0].props.open, false,
  "ordinary browsing should not expand an unrelated event");
  const next = button("Next 100 recorded events").props.onClick();
  const nextRequest = requests.filter((item) => item.url.includes("/local-events?")).at(-1);
  assert.match(nextRequest.url, /after_ordinal=1499/);
  await resolveJson(nextRequest, { coverage: "complete_recorded",
    sourceRevision: "a".repeat(64), sourceEventCount: 1600,
    events: [{ sourceOrdinal: 1500, eventId: "middle-2", turnId: "turn-1",
      type: "tool_result", status: "completed", occurredAt: "2026-10-20T00:00:01Z",
      attributes: {} }], nextCursor: null,
  });
  await next;
  assert.match(textOf(render(component.type, sampleHooks, component.props)),
    /Source event #\s*1501/);
  const analyze = button("Analyze recorded run").props.onClick();
  const analysisRequest = requests.find((item) => item.url.endsWith("/recorded-analysis"));
  assert.ok(analysisRequest);
  await resolveJson(analysisRequest, { coverage: "complete_recorded",
    sourceRevision: "a".repeat(64), recordedTurns: 2, recordedEvents: 1600,
    metrics: { tool_calls: 40, tool_errors: 1, command_failures: 1 },
    findings: [{ code: "command_failed", message: "One command failed.",
      evidenceEventIds: ["middle-2"], firstEvidenceCursor: {
        sourceOrdinal: 1499, eventId: "middle-1",
      } }], findingsTruncated: false,
    recommendations: [{ code: "review_tool_failure", status: "suggested",
      observation: "A tool result reported failure.",
      action: "Inspect the linked tool arguments and results before changing the workflow.",
      evidenceEventIds: ["middle-2"], firstEvidenceCursor: {
        sourceOrdinal: 1499, eventId: "middle-1",
      } }],
  });
  await analyze;
  assert.match(textOf(render(component.type, sampleHooks, component.props)),
    /Analysis of\s+2\s+recorded\s+turns\s+and\s+1,600\s+recorded events/);
  assert.match(textOf(render(component.type, sampleHooks, component.props)),
    /Suggested next step.*Inspect the linked tool arguments and results/s);
  const supporting = button("Inspect supporting event").props.onClick();
  const supportRequest = requests.filter((item) => item.url.includes("/local-events?")).at(-1);
  assert.match(supportRequest.url, /after_ordinal=1499/);
  await resolveJson(supportRequest, { coverage: "complete_recorded",
    sourceRevision: "a".repeat(64), sourceEventCount: 1600,
    events: [{ sourceOrdinal: 1500, eventId: "middle-2", turnId: "turn-1",
      type: "command", status: "failed", occurredAt: "2026-10-20T00:00:01Z",
      attributes: { exit_code: 1 } }], nextCursor: null,
  });
  await supporting;
  const focusedPage = findAll(render(component.type, sampleHooks, component.props),
    (node) => node.type === "div" && node.props["aria-label"] === "Recorded event page")[0];
  assert.ok(focusedPage, "a supporting-event click should expose a focusable recorded page");
  const focusedRow = findAll(focusedPage, (node) => node.type === "details"
    && /Source event #\s*1501/.test(textOf(node)))[0];
  assert.equal(focusedRow?.props.open, true, "the specifically requested event should open");
  let scrolls = 0;
  focusedPage.props.ref.current = { scrollIntoView() { scrolls += 1; } };
  sampleHooks.flushEffects();
  assert.equal(scrolls, 1, "the loaded event page should move into view once");
  const evidence = button("Inspect recorded evidence").props.onClick();
  const evidenceRequest = requests.filter((item) => item.url.includes("/local-events?")).at(-1);
  assert.match(evidenceRequest.url, /after_ordinal=1499/);
  await resolveJson(evidenceRequest, { coverage: "complete_recorded",
    sourceRevision: "a".repeat(64), sourceEventCount: 1600,
    events: [{ sourceOrdinal: 1500, eventId: "middle-2", turnId: "turn-1",
      type: "command", status: "failed", occurredAt: "2026-10-20T00:00:01Z",
      attributes: { exit_code: 1 } }], nextCursor: null,
  });
  await evidence;
});

test("issue navigation checks a missing prefix finding in retained local history", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = { ...runListRow("middle-issue-run"), sourceKind: "codex",
    sourcePartial: true, eventCount: 1500, metrics: { tool_calls: 749 }, findings: [] };
  const props = { url: "/api/runs", focusRunIds: ["middle-issue-run"],
    selectedRunId: "middle-issue-run", findingCode: "command_failed" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, [run.runId], 1),
    runs: [{ ...run, sourcePartial: undefined }], selectedRun: run });
  const picker = findAll(render(ui.Runs, hooks, props),
    (node) => node.type === "button" && /codex/.test(textOf(node))
      && /1500/.test(textOf(node)));
  assert.equal(picker.length, 0,
    "a compact list row without coverage must not imply 1500 is the whole-run count");
  const detail = findAll(render(ui.Runs, hooks, props),
    (node) => node.type?.name === "RunDetail")[0];
  const detailTree = render(detail.type, createHooks(), detail.props);
  const quickViewText = textOf(detailTree);
  const prefixDisclosure = findAll(detailTree, (node) => node.type === "details"
    && node.props["aria-label"] === "Historical prefix details")[0];
  assert.ok(prefixDisclosure, "the bounded prefix must not compete with the full recorded-run report");
  assert.equal(prefixDisclosure.props.open, undefined, "prefix details start collapsed");
  assert.match(quickViewText, /749\s+tool calls in stored prefix/);
  assert.doesNotMatch(quickViewText,
    /selected finding is no longer present/);
  assert.doesNotMatch(quickViewText, /No evidence-backed action suggested for this run/);
  const component = findAll(render(detail.type, createHooks(), detail.props),
    (node) => node.type?.name === "RecentLocalEvents")[0];
  const sampleHooks = createEffectHooks();
  render(component.type, sampleHooks, component.props);
  sampleHooks.flushEffects();
  const sampleRequest = requests.find((item) => item.url.endsWith("/recent-local-events"));
  assert.ok(sampleRequest, "the selected issue should load its recorded source");
  await resolveJson(sampleRequest, { schema: "local-recent-window-v2",
    durableFacts: true, runId: run.runId, sourceRevision: "a".repeat(64),
    sourceEventCount: 1600, sourceRecordCount: 1610,
    canonicalPrefixEvents: 1499, omittedMiddleEvents: 51,
    events: [], turns: [], findings: [] });
  render(component.type, sampleHooks, component.props);
  sampleHooks.flushEffects();
  const analysisRequest = requests.find((item) => item.url.endsWith("/recorded-analysis"));
  assert.ok(analysisRequest, "the selected issue should analyze the admitted run");
  await resolveJson(analysisRequest, { coverage: "complete_recorded",
    sourceRevision: "a".repeat(64), recordedTurns: 1, recordedEvents: 1600,
    metrics: { command_failures: 1 }, findings: [{ code: "command_failed",
      message: "One command failed.", evidenceEventIds: ["middle-failure"],
      firstEvidenceCursor: { sourceOrdinal: 1549, eventId: "previous" } }],
    recommendations: [], findingsTruncated: false });
  const analyzedText = textOf(render(component.type, sampleHooks, component.props));
  assert.match(analyzedText, /One command failed/);
  assert.match(analyzedText, /Analysis of\s+1\s+recorded\s+turn\s+and\s+1,600\s+recorded events/);
  assert.ok(analyzedText.indexOf("Analysis of") < analyzedText.indexOf("Recent local-history sample"),
    "the full recorded-run report should lead the sample details");
});

test("recent V2 window shows scoped findings and incomplete-turn eligibility", async () => {
  const ui = await loadUiModule();
  const listHooks = createEffectHooks();
  const requests = deferredFetches();
  const opened = [];
  const props = { url: "/api/runs", focusRunIds: ["large-run"], selectedRunId: "large-run",
    onOpenRecentEvent: (reference) => opened.push(reference) };
  render(ui.Runs, listHooks, props);
  listHooks.flushEffects();
  const run = { ...runListRow("large-run"), sourceKind: "codex", eventCount: 1500,
    sourcePartial: true };
  await resolveJson(requests[0], { ...runListPage(0, ["large-run"], 1), runs: [run] });
  const detail = findAll(render(ui.Runs, listHooks, props),
    (node) => node.type?.name === "RunDetail")[0];
  const detailTree = render(detail.type, createHooks(), detail.props);
  assert.match(textOf(detailTree), /Recorded prefix outcome\s*:/);
  const component = findAll(detailTree,
    (node) => node.type?.name === "RecentLocalEvents")[0];
  const hooks = createEffectHooks();
  let tree = render(component.type, hooks, component.props);
  hooks.flushEffects();
  tree = render(component.type, hooks, component.props);
  const pending = findAll(tree, (node) => node.type === "button" &&
    textOf(node) === "View recent sample")[0].props.onClick();
  const request = requests.find((item) => item.url === "/api/runs/large-run/recent-local-events");
  await resolveJson(request, { schema: "local-recent-window-v2", durableFacts: true, runId: "large-run",
    sourceRevision: "f".repeat(64), capturedAt: "2026-10-19T00:00:00Z",
    sourceRecordCount: 1200, sourceTurnCount: 1001, sourceEventCount: 1300,
    omittedMiddleEvents: 200,
    turns: [
      { turnId: "complete", sourceSequence: 999, status: "completed", sourceEventCount: 1,
        selectedEventCount: 1, eligibleForFindings: true },
      { turnId: "partial", sourceSequence: 1000, status: "unknown", sourceEventCount: 3,
        selectedEventCount: 1, eligibleForFindings: false },
    ],
    events: [
      { sourceOrdinal: 1296, eventId: "failed", turnId: "complete", type: "tool_result",
        status: "failed", provenance: "codex:tool", privacy: "metadata", attributes: {} },
      { sourceOrdinal: 1299, eventId: "partial-event", turnId: "partial", type: "tool_result",
        status: "failed", provenance: "codex:tool", privacy: "metadata", attributes: {} },
    ],
    findings: [{ code: "tool_failed", eventId: "failed", turnId: "complete" }],
  });
  await pending;
  tree = render(component.type, hooks, component.props);
  assert.match(textOf(tree), /1\s+recorded\s+issue\s+in complete selected turns/);
  assert.match(textOf(tree), /Turn #\s*1001.*partial selection/s);
  assert.match(textOf(tree), /Explicit recorded failures can enter recurring issue monitoring/);
  assert.doesNotMatch(textOf(tree), /does not enter recurring monitoring or LLM evaluation/);
  const exactLinks = findAll(tree, (node) => node.type === "button" &&
    textOf(node) === "Open exact event link");
  assert.ok(exactLinks.length >= 1);
  exactLinks[0].props.onClick();
  assert.deepEqual(opened[0], { runId: "large-run", revision: "f".repeat(64),
    turnId: "complete", eventId: "failed" });
});

test("a revision-qualified recent event hash loads and opens that exact event", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs", focusRunIds: ["large-run"], selectedRunId: "large-run",
    recentRevision: "a".repeat(64), recentTurnId: "turn-9", recentEventId: "event-9" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const run = { ...runListRow("large-run"), sourceKind: "codex", sourcePartial: true,
    eventCount: 1500 };
  await resolveJson(requests[0], { ...runListPage(0, ["large-run"], 1), runs: [run] });
  const detail = findAll(render(ui.Runs, hooks, props),
    (node) => node.type?.name === "RunDetail")[0];
  const component = findAll(render(detail.type, createHooks(), detail.props),
    (node) => node.type?.name === "RecentLocalEvents")[0];
  const sampleHooks = createEffectHooks();
  render(component.type, sampleHooks, component.props);
  sampleHooks.flushEffects();
  const exact = requests.find((item) => item.url.includes("recent-local-events?"));
  assert.ok(exact);
  assert.match(exact.url, /revision=a{64}/);
  assert.match(exact.url, /turn_id=turn-9&event_id=event-9/);
  const event = { sourceOrdinal: 9, eventId: "event-9", turnId: "turn-9",
    type: "tool_result", status: "failed", occurredAt: "2026-10-19T00:00:00Z",
    provenance: "codex:tool", privacy: "metadata", attributes: {} };
  await resolveJson(exact, { schema: "local-recent-window-v2", runId: "large-run",
    sourceRevision: "a".repeat(64), capturedAt: "2026-10-19T00:00:00Z",
    sourceEventCount: 10, sourceRecordCount: 20, omittedMiddleEvents: 0,
    events: [event], turns: [], findings: [], focusedEvent: event });
  const tree = render(component.type, sampleHooks, component.props);
  const selected = findAll(tree, (node) => node.type === "details" && node.props.open);
  assert.equal(selected.length, 1);
  assert.match(textOf(selected[0]), /Source event #\s*10/);
});

test("switching exact recent events while the first fetch is pending opens the latest event", async () => {
  const ui = await loadUiModule();
  const listHooks = createEffectHooks();
  const requests = deferredFetches();
  const props = { url: "/api/runs", focusRunIds: ["large-run"], selectedRunId: "large-run" };
  render(ui.Runs, listHooks, props);
  listHooks.flushEffects();
  const run = { ...runListRow("large-run"), sourceKind: "codex", eventCount: 1500 };
  await resolveJson(requests[0], { ...runListPage(0, ["large-run"], 1), runs: [run] });
  const detail = findAll(render(ui.Runs, listHooks, props),
    (node) => node.type?.name === "RunDetail")[0];
  const component = findAll(render(detail.type, createHooks(), detail.props),
    (node) => node.type?.name === "RecentLocalEvents")[0];
  const sampleHooks = createEffectHooks();
  const revision = "a".repeat(64);
  const referenceA = { revision, turnId: "turn-1", eventId: "event-1" };
  const referenceB = { revision, turnId: "turn-2", eventId: "event-2" };
  render(component.type, sampleHooks, { ...component.props, recentReference: referenceA });
  sampleHooks.flushEffects();
  const first = requests.find((request) => request.url.includes("event_id=event-1"));
  assert.ok(first);
  render(component.type, sampleHooks, { ...component.props, recentReference: referenceB });
  sampleHooks.flushEffects();
  const second = requests.find((request) => request.url.includes("event_id=event-2"));
  assert.ok(second, "the new exact reference must fetch even while the old request is pending");
  const eventB = { sourceOrdinal: 9, eventId: "event-2", turnId: "turn-2",
    type: "command", status: "failed", occurredAt: "2026-10-19T00:00:00Z",
    provenance: "codex:tool", privacy: "metadata", attributes: {} };
  await resolveJson(second, { schema: "local-recent-window-v2", runId: "large-run",
    sourceRevision: revision, events: [eventB], turns: [], findings: [],
    focusedEvent: eventB });
  await resolveJson(first, { schema: "local-recent-window-v2", runId: "large-run",
    sourceRevision: revision, events: [{ ...eventB, eventId: "event-1", turnId: "turn-1",
      type: "tool_result" }],
    turns: [], findings: [] });
  const tree = render(component.type, sampleHooks,
    { ...component.props, recentReference: referenceB });
  assert.match(textOf(tree), /Source event #\s*10/);
  assert.match(textOf(tree), /command/);
  assert.doesNotMatch(textOf(tree), /tool result/);
  assert.equal(findAll(tree, (node) => node.type === "details" && node.props.open).length, 1);
});

test("a finding link opens its run with the finding, action, and evidence before technical detail", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = runListRow("run-a");
  run.findings = [{ code: "test_failed", severity: "error", message: "1 test execution(s) reported failure.",
    evidenceEventIds: ["event-failure", "event-followup"] },
    { code: "tool_error", severity: "error", message: "A tool failed.", evidenceEventIds: ["event-tool"] }];
  run.recommendations = [{ code: "inspect_test", status: "suggested", observation: "A test failed.",
    action: "Inspect and rerun the test.", rationale: "The last recorded test failed.",
    tradeoff: "A later test may have passed.", evidenceEventIds: ["event-failure"] }];
  const openedTraces = [];
  const props = { url: "/api/runs", focusRunIds: ["run-a"], selectedRunId: "run-a",
    findingCode: "test_failed", onBrowseRecordedGroup: (group) => browsed.push(group),
    onOpenTrace: (traceId) => openedTraces.push(traceId) };
  const browsed = [];
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["run-a"], 1), runs: [run] });
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const request = requests.find((item) => item.url.includes("event_id=event-failure"));
  assert.ok(request, "the linked event must load even when it is outside the first page");
  await resolveJson(request, { runId: "run-a", turns: [], events: [{ eventId: "event-failure",
    timelineIndex: 3, type: "test_result", status: "failed", occurredAt: "2026-09-01T00:00:00Z",
    attributes: { failed: 1 }, provenance: "synthetic:fixture", privacy: "metadata",
    traceId: "trace-failure" }],
    page: { available: 1, shown: 1, offset: 0, limit: 100, truncated: false } });
  const tree = render(ui.Runs, hooks, props);
  const story = findAll(tree, (node) => node.type?.name === "FocusedIssueStory")[0];
  assert.ok(story, "a finding deep link should lead with the issue story");
  const storyTree = render(story.type, createHooks(), story.props);
  const storyText = textOf(storyTree);
  assert.match(storyText, /What happened.*1 test execution reported failure/s);
  assert.match(storyText, /EVENT.*#4 · test result · failed/s);
  assert.match(storyText, /Recorded evidence.*Suggested next action/s);
  assert.equal(findAll(storyTree, (node) => node.type?.name === "EventRow" && node.props.focused).length, 1,
    "the linked event should be directly inspectable in the first-screen story");
  assert.equal(findAll(storyTree, (node) => node.type?.name === "EventRow")[0].props.idPrefix, "lead-",
    "the visible event must not duplicate the ID in the full run browser");
  const lead = findAll(storyTree, (node) => node.type?.name === "EventRow")[0];
  assert.equal(lead.props.event.traceId, "trace-failure");
  const leadTree = render(lead.type, createEffectHooks(), lead.props);
  const leadTrace = findAll(leadTree, (node) => node.type === "button"
    && textOf(node).includes("trace-failure"))[0];
  assert.ok(leadTrace, "the first-screen evidence should expose its recorded Trace");
  leadTrace.props.onClick({ preventDefault() {}, stopPropagation() {} });
  assert.deepEqual(openedTraces, ["trace-failure"], "the first-screen Trace action must navigate");
  assert.match(storyText, /Inspect and rerun the test/);
  assert.match(storyText, /After the fix/);
  assert.match(storyText, /Compare runs with the same recorded source and agent/);
  findAll(storyTree, (node) => node.type === "button" &&
    /Compare runs with the same recorded source and agent/.test(textOf(node)))[0].props.onClick();
  assert.deepEqual(browsed, [{ sourceKind: "codex", agentName: "run-a" }]);
  const fullDetail = findAll(tree, (node) => node.type === "details"
    && node.props.className === "verdict-investigation-details")[0];
  assert.equal(fullDetail.props.open, undefined,
    "the technical run browser should be available without displacing the first-screen story");
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  assert.equal(detail.props.prioritizeDetail, true);
  const rendered = render(detail.type, createHooks(), detail.props);
  assert.match(rendered.props.className, /order-1 lg:order-2/);
  assert.equal(findAll(rendered, (node) => node.type === "details"
    && node.props["aria-label"] === "Historical prefix details").length, 0,
  "a complete run should keep its normal investigation layout");
  const content = textOf(rendered);
  assert.match(content, /What happened: Test failed/);
  assert.match(content, /Run outcome\s*:\s+completed/);
  assert.doesNotMatch(content, /Run status unavailable/);
  assert.match(content, /1 test execution reported failure/);
  assert.doesNotMatch(content, /test execution\(s\)/);
  assert.ok(content.indexOf("1 test execution reported failure") < content.indexOf("Inspect and rerun the test."));
  assert.ok(content.indexOf("Inspect and rerun the test.") < content.indexOf("Technical coverage and further evaluation"));
  assert.match(content, /Recorded evidence/);
  const evidence = findAll(rendered, (node) => node.type?.name === "EventRow" && node.props.focused)[0];
  assert.equal(evidence.props.event.eventId, "event-failure");
  const expanded = render(evidence.type, createEffectHooks(), evidence.props);
  assert.match(textOf(expanded), /Run ID\s+run-a/);
  assert.match(textOf(expanded), /Event ID\s+event-failure/);
  assert.match(content, /Event\s+1\s+of\s+2/);
  findAll(rendered, (node) => node.type === "button" && textOf(node) === "Next event")[0].props.onClick();
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.ok(requests.some((item) => item.url.includes("event_id=event-followup")),
    "every linked evidence event must be reachable from the focused finding");
  detail.props.onEventPage(100);
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  assert.ok(requests.some((item) => item.url.includes("event_offset=100")
    && !item.url.includes("event_id=")), "paging must leave the focused event and open the requested page");
});

test("a failed Agent Run without event findings explains its recorded status", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = { ...runListRow("failed-run"), status: "failed", eventCount: 0,
    findings: [] };
  const props = { url: "/api/runs", focusRunIds: ["failed-run"],
    selectedRunId: "failed-run", eventId: null };
  render(ui.Runs, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["failed-run"], 1),
    runs: [run] });
  render(ui.Runs, hooks, props); hooks.flushEffects();
  const detailRequest = requests.find((item) => item.url.includes("/api/runs/failed-run"));
  assert.ok(detailRequest);
  await resolveJson(detailRequest, { runId: "failed-run", turns: [], events: [],
    page: { available: 0, shown: 0, offset: 0, limit: 100, truncated: false } });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  const content = textOf(render(detail.type, createHooks(), detail.props));
  assert.match(content, /Recorded execution status\s*:\s+failed/);
  assert.match(content, /Inspect the run's recorded status and timeline/);
  assert.doesNotMatch(content, /No deterministic findings in this run/);
});

test("recorded issue evidence uses failure styling even without failed status", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = runListRow("run-timeout");
  run.findings = [{ code: "tool_error", severity: "error", message: "A tool timed out.",
    evidenceEventIds: ["event-timeout"] }];
  const props = { url: "/api/runs", focusRunIds: ["run-timeout"],
    selectedRunId: "run-timeout", routedEventId: "event-timeout" };
  render(ui.Runs, hooks, props); hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["run-timeout"], 1), runs: [run] });
  render(ui.Runs, hooks, props); hooks.flushEffects();
  const request = requests.find((item) => item.url.includes("event_id=event-timeout"));
  assert.ok(request);
  await resolveJson(request, { runId: "run-timeout", turns: [], events: [{
    eventId: "event-timeout", timelineIndex: 0, type: "tool_result", status: "timed_out",
    issueCode: "tool_error", occurredAt: "2026-10-19T00:00:00Z", attributes: { is_error: false },
    provenance: "synthetic:fixture", privacy: "metadata",
  }], page: { available: 1, shown: 1, offset: 0, limit: 100, truncated: false } });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  const rendered = render(detail.type, createHooks(), detail.props);
  const evidence = findAll(rendered, (node) => node.type?.name === "EventRow")[0];
  assert.ok(evidence);
  const row = render(evidence.type, createHooks(), evidence.props);
  assert.equal(row.props.style.borderColor, "#ff6b6b");
});

test("a repeated-call finding gives a review action linked to its recorded events", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = runListRow("run-loop");
  run.findings = [{ code: "possible_tool_loop", severity: "warning",
    message: "1 tool-call pattern repeated at least 4 times within a turn (6 recorded calls total).",
    evidenceEventIds: ["call-a", "call-b"] }];
  run.recommendations = [];
  const props = { url: "/api/runs", focusRunIds: ["run-loop"], selectedRunId: "run-loop",
    findingCode: "possible_tool_loop" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["run-loop"], 1), runs: [run] });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  const focused = [];
  const view = render(detail.type, createHooks(), { ...detail.props,
    onFocusEvent: (eventId) => focused.push(eventId) });
  const content = textOf(view);
  assert.match(content, /1 tool-call pattern repeated at least 4 times/);
  assert.match(content, /Inspect the repeated calls and their results/);
  assert.match(content, /does not establish wasted calls or preserved quality/);
  findAll(view, (node) => node.type === "button" && textOf(node) === "Inspect recorded evidence")[0]
    .props.onClick();
  assert.deepEqual(focused, ["call-a"]);
});

test("a recorded tool failure without a richer recommendation still has an evidence-linked recheck", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const run = runListRow("run-tool");
  run.findings = [{ code: "tool_error", severity: "error", message: "1 tool result reported failure.",
    evidenceEventIds: ["failed-tool"] }];
  run.recommendations = [];
  render(ui.Runs, hooks, { url: "/api/runs", focusRunIds: ["run-tool"],
    selectedRunId: "run-tool", findingCode: "tool_error" });
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["run-tool"], 1), runs: [run] });
  const tree = render(ui.Runs, hooks, { url: "/api/runs", focusRunIds: ["run-tool"],
    selectedRunId: "run-tool", findingCode: "tool_error" });
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  const focused = [];
  const view = render(detail.type, createHooks(), { ...detail.props,
    onFocusEvent: (eventId) => focused.push(eventId) });
  const content = textOf(view);
  assert.match(content, /Inspect the tool error or timeout/);
  assert.match(content, /Repeat the operation only after reviewing its inputs/);
  assert.doesNotMatch(content, /No action is linked/);
  findAll(view, (node) => node.type === "button" && textOf(node) === "Inspect recorded evidence")[0]
    .props.onClick();
  assert.deepEqual(focused, ["failed-tool"]);
});

test("a capture job that fails is reported instead of completing the setup", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const completed = [];
  const props = { configUrl: "/api/config", onComplete: (source) => completed.push(source), agentSummary: {} };
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback) => realSetTimeout(callback, 0);
  try {
    render(ui.SetupWizard, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], { setupToken: "setup-token" });
    await resolveJson(requests[1], { job: null });
    const button = (label) => findAll(render(ui.SetupWizard, hooks, props),
      (node) => node.type === "button" && textOf(node) === label)[0];
    const previewing = button("Preview sources").props.onClick();
    await resolveJson(requests[2], { claude: { files: 1 }, codex: { files: 0 } });
    await previewing;
    const capturing = button("Approve and capture").props.onClick();
    await resolveJson(requests[3], { job: { jobId: "job-2", state: "running", filesDone: 0, filesTotal: 1 } });
    await untilRequests(requests, 5, realSetTimeout);
    await resolveJson(requests[4], { job: { jobId: "job-2", state: "failed", filesDone: 0, filesTotal: 1, error: "capture_failed" } });
    await capturing;
    const alerts = findAll(render(ui.SetupWizard, hooks, props), (node) => node.props?.role === "alert").map(textOf).join(" ");
    assert.match(alerts, /capture_failed/);
    assert.deepEqual(completed, []);
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});

test("a running setup capture can be cancelled and refreshes committed evidence", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const refreshed = [];
  const props = { configUrl: "/api/config", agentSummary: {}, onRefresh: () => refreshed.push(true) };
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback) => realSetTimeout(callback, 0);
  try {
    render(ui.SetupWizard, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], { setupToken: "setup-token" });
    await resolveJson(requests[1], { job: { jobId: "job-cancel", state: "running",
      filesDone: 1, filesTotal: 2 } });
    const cancel = findAll(render(ui.SetupWizard, hooks, props),
      (node) => node.type === "button" && textOf(node) === "Cancel capture")[0];
    assert.ok(cancel);
    const cancelling = cancel.props.onClick();
    assert.equal(requests[2].url, "/api/setup/capture/job-cancel/cancel");
    assert.equal(requests[2].options.headers["X-Verdict-Setup"], "setup-token");
    await resolveJson(requests[2], { job: { jobId: "job-cancel", state: "running",
      cancelRequested: true } });
    await cancelling;
    await untilRequests(requests, 4, realSetTimeout);
    await resolveJson(requests[3], { job: { jobId: "job-cancel", state: "cancelled",
      error: "capture_cancelled", summary: { files: 2, stored: 1, skipped: 0 } } });
    await new Promise((resolve) => realSetTimeout(resolve, 0));
    assert.deepEqual(refreshed, [true]);
    assert.match(textOf(render(ui.SetupWizard, hooks, props)), /capture was cancelled/i);
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});

test("a reloaded setup page picks up the capture that is still running", async () => {
  const ui = await loadUiModule();
  const hooks = createEffectHooks();
  const requests = deferredFetches();
  const completed = [];
  const props = { configUrl: "/api/config", onComplete: (source) => completed.push(source), agentSummary: {} };
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback) => realSetTimeout(callback, 0);
  try {
    render(ui.SetupWizard, hooks, props);
    hooks.flushEffects();
    await resolveJson(requests[0], { setupToken: "setup-token" });
    await resolveJson(requests[1], { job: { jobId: "job-9", state: "running", filesDone: 40, filesTotal: 90 } });
    const status = () => findAll(render(ui.SetupWizard, hooks, props),
      (node) => node.props?.role === "status").map(textOf).join(" ");
    assert.match(status(), /Importing 40 of 90 history files/);
    await untilRequests(requests, 3, realSetTimeout);
    assert.match(requests[2].url, /\/api\/setup\/capture\/status\?job=job-9$/);
    await resolveJson(requests[2], { job: { jobId: "job-9", state: "completed", filesDone: 90, filesTotal: 90,
      summary: { stored: 90 }, analysis: { status: "completed" } } });
    for (let index = 0; index < 20 && completed.length === 0; index += 1) await new Promise((resolve) => realSetTimeout(resolve, 1));
    assert.deepEqual(completed, ["local"]);
    assert.equal(status(), "");
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});

test("reloaded capture progress and failure stay visible with an observed store", async () => {
  const ui = await loadUiModule();
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback) => realSetTimeout(callback, 0);
  try {
    for (const agentSummary of [
      { totalAgentRuns: 40, totalTraces: 0 },
      { totalAgentRuns: 0, totalTraces: 5 },
      { storageBackend: "sqlite", totalAgentRuns: 0, totalTraces: 0 },
    ]) {
      const hooks = createEffectHooks();
      const requests = deferredFetches();
      const props = { configUrl: "/api/config", onComplete: () => {}, agentSummary };
      render(ui.SetupWizard, hooks, props);
      hooks.flushEffects();
      await resolveJson(requests[0], { setupToken: "setup-token" });
      await resolveJson(requests[1], { job: { jobId: "job-10", state: "running", filesDone: 40, filesTotal: 90 } });
      const notices = (role) => findAll(render(ui.SetupWizard, hooks, props),
        (node) => node.props?.role === role).map(textOf).join(" ");
      assert.match(notices("status"), /Importing 40 of 90 history files/);
      await untilRequests(requests, 3, realSetTimeout);
      await resolveJson(requests[2], { job: { jobId: "job-10", state: "failed", error: "capture_failed" } });
      for (let attempt = 0; attempt < 20 && !notices("alert"); attempt += 1) {
        await new Promise((resolve) => realSetTimeout(resolve, 1));
      }
      assert.match(notices("alert"), /capture_failed/);
    }
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});
