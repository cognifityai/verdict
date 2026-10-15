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
  const source = `${await readFile(UI_SOURCE, "utf8")}\nexport { Dashboard, Overview, Traces, TraceDetail, TabHelp, Judge, Compare, ManagementReport, mountedApiUrl, Monitor };\nexport { useOperations } from "./Operations.jsx";\nexport { RegistryView } from "./Registry.jsx";\nexport { EvaluatorLab } from "./EvaluatorLab.jsx";\nexport { ConversationEvaluation } from "./ConversationEvaluation.jsx";\nexport { MatchedConversationCompare } from "./MatchedConversationCompare.jsx";\nexport { Runs, TurnAnalysis } from "./Runs.jsx";\nexport { Usage, DayExplanation, DayAiExplanation } from "./Usage.jsx";\nexport { Experiments } from "./Experiments.jsx";\nexport { ScopedEvaluate } from "./ScopedEvaluate.jsx";\nexport { Insights } from "./Insights.jsx";\nexport { SetupWizard } from "./SetupWizard.jsx";`;
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
      { day: "2026-10-08", turns: 2, tokenKnownTurns: 1, totalTokens: 300, pricedTurns: 1, listPriceUsd: 0.02 },
    ],
    selectedDay: "2026-10-08", dayModels: [], sessions: [],
    sessionsTruncated: false, selectedSession: null,
  };
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  assert.equal(requests[0].url, "/api/usage");
  await resolveJson(requests[0], base);
  let tree = render(ui.Usage, hooks, props);
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
    { day: "2026-10-08", turns: 1, tokenKnownTurns: 1, totalTokens: 300, pricedTurns: 0 },
  ];
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { window: { complete: true }, days, selectedDay: days[1].day,
    dayModels: [], daySources: [], sessions: [], selectedSession: null });
  let tree = render(ui.Usage, hooks, props);
  const chart = findAll(tree, (node) => node.type?.name === "UsageChart")[0];
  const chartTree = render(chart.type, createHooks(), chart.props);
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

test("agent Overview starts with token and issue charts before technical detail", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.meta.totalAgentRuns = 2;
  data.monitor = { state: "not_configured", active: null, candidate: null };
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    const usage = findAll(tree, (node) => node.type?.name === "Usage");
    const insights = findAll(tree, (node) => node.type?.name === "Insights");
    assert.equal(usage.length, 1);
    assert.equal(usage[0].props.compact, true);
    assert.equal(insights.length, 1);
    assert.match(textOf(tree), /Technical coverage and charts/);
  } finally { delete globalThis.window; }
});

test("Overview puts investigation before monitoring status", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.meta.totalAgentRuns = 2;
  data.monitor = { state: "not_configured", active: null, candidate: null };
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard", search: "" },
    history: { pushState() {}, replaceState() {} }, addEventListener() {}, removeEventListener() {} };
  try {
    const assertInvestigationBeforeStatus = (tree, label) => {
      const nodes = findAll(tree, () => true);
      const statusIndex = nodes.findIndex((node) => node.type?.name?.startsWith("Panel") && textOf(node).includes(label));
      const issuesIndex = nodes.findIndex((node) => node.type?.name === "Insights");
      const usageIndex = nodes.findIndex((node) => node.type?.name === "Usage");
      assert.ok(issuesIndex >= 0 && usageIndex > issuesIndex && statusIndex > usageIndex);
    };
    let tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    assert.doesNotMatch(textOf(tree), /Monitoring not configured/);

    data.monitor = { active: null, candidate: { state: "candidate",
      snapshot: { comparison: { status: "no_alert", metrics: [] } } } };
    tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    assertInvestigationBeforeStatus(tree, "Historical comparison found no alert");

    data.monitor = { active: { state: "active",
      snapshot: { comparison: { status: "alert", metrics: [{ alert: true }] } } }, candidate: null };
    tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    const nodes = findAll(tree, () => true);
    const alertIndex = nodes.findIndex((node) => node.type?.name?.startsWith("Panel")
      && textOf(node).includes("Active monitor found 1 alert"));
    const issuesIndex = nodes.findIndex((node) => node.type?.name === "Insights");
    assert.ok(alertIndex >= 0 && alertIndex < issuesIndex, "an active alert remains prominent");
  } finally { delete globalThis.window; }
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
  assert.equal(bars.length, 30);
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
  assert.match(textOf(tree), /token trend is unavailable/i);
  assert.match(textOf(tree), /bounded and partial/i);
  assert.equal(findAll(tree, (node) => node.type?.name === "UsageChart").length, 0);
  assert.equal(findAll(tree, (node) => node.type === "button" && textOf(node).includes("Est. list price"))[0].props.disabled, true);
  const inspect = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Inspect sessions with missing totals"))[0];
  inspect.props.onClick();
  render(ui.Usage, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[1], snapshot);
  tree = render(ui.Usage, hooks, props);
  assert.match(textOf(tree), /Sessions active on\s+2026-10-07\s+UTC/);
  assert.match(textOf(tree), /new-agent/);
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
  const props = { url: "/api/insights", onOpenRuns: (finding) => opened.push(finding.code) };
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
  assert.match(textOf(tree), /8 of 10 captured runs/i);
  assert.match(textOf(tree), /This view is partial/i);
  assert.equal(findAll(tree, (node) => node.type === "details").length, 2);
  issueButtons[0].props.onClick();
  const gapButton = findAll(tree, (node) => node.type === "button" && textOf(node).includes("Run outcome unavailable"))[0];
  gapButton.props.onClick();
  assert.deepEqual(opened, ["tool_error", "run_status_unknown"]);
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
    return render(ui.Dashboard, createHooks(), { data: bundle("judge-a"), source: "live" });
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
      render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live" });
      hooks.flushEffects();
      const tree = render(ui.Dashboard, hooks, { data: bundle("judge-a"), source: "live" });
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
    assert.ok(findAll(tree, (node) => node.type === "button" && node.props?.title === "Open report").length);
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

test("overview and navigation use only the active monitor as current drift", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.driftSignals = [{ id: "signal-1", direction: "regression" }];
  data.driftRun = { id: "run-1", signalCount: 1 };
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

test("overview summarizes an unchanged historical check without raw metric rows", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.monitor = { active: null, candidate: {
    state: "candidate",
    snapshot: {
      manifest: { reference_unit_ids: Array(80).fill("r"), current_unit_ids: Array(20).fill("c"), prospective_open: false },
      comparison: { status: "no_alert", metrics: [
        { group_id: "provider_model:opaque", metric: "provider_error", alert: false,
          reference_value: 0, current_value: 0 },
      ], groups: [{ group_id: "provider_model:opaque", label: "a provider / model" }] },
    },
  } };

  const text = textOf(render(ui.Overview, createHooks(), { data }));
  assert.match(text, /historical comparison found no alert/i);
  assert.match(text, /not active monitoring/i);
  assert.doesNotMatch(text, /Provider error rate|0\.0% → 0\.0%/);
});

test("overview historical check opens Monitor history without treating it as active", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.monitor = { active: null, candidate: { state: "candidate",
    snapshot: { comparison: { status: "no_alert", metrics: [] } } } };
  let pushed = null;
  globalThis.window = { location: { hash: "#tab=overview&section=summary", pathname: "/dashboard" },
    history: { pushState: (_state, _title, url) => { pushed = url; }, replaceState() {} },
    addEventListener() {}, removeEventListener() {} };
  try {
    const tree = render(ui.Dashboard, createHooks(), { data });
    const details = findAll(tree, (node) => node.type === "button"
      && textOf(node).includes("Open Monitor details"))[0];
    assert.ok(details);
    details.props.onClick();
    assert.equal(pushed, "#tab=monitor&section=history");
    assert.doesNotMatch(textOf(tree), /0\.0% → 0\.0%/);
  } finally { delete globalThis.window; }
});

test("bundled sample demonstrates the current Monitor instead of only legacy signals", async () => {
  const ui = await loadUiModule();
  const dashboard = render(ui.Dashboard, createHooks(), {});
  const monitorButton = findAll(dashboard,
    (node) => node.type === "button" && textOf(node).includes("Monitor"))[0];
  assert.equal(textOf(monitorButton).trim(), "Monitor 1");
  assert.match(textOf(dashboard), /Active monitor found|No alert in the latest active comparison/i);
});

test("an inconsistent legacy run does not advertise its stale signal count", async () => {
  const ui = await loadUiModule();
  const data = bundle("judge-a");
  data.driftRun = { id: "inconsistent-run", signalCount: 2 };
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

test("dashboard shows four primary tasks and keeps report and settings accessible", async () => {
  const ui = await loadUiModule();
  const tree = render(ui.Dashboard, createHooks(), { data: bundle("judge-a") });
  const navigation = findAll(tree, (node) => node.type === "nav")[0];
  const labels = textOf(navigation);

  for (const label of ["Overview", "Investigate", "Evaluate", "Monitor"]) {
    assert.match(labels, new RegExp(label));
  }
  assert.doesNotMatch(labels, /Report|Settings/);
  assert.ok(findAll(tree, (node) => node.type === "button" && node.props?.title === "Open report").length);
  assert.ok(findAll(tree, (node) => node.type === "button" && node.props?.title === "Open settings").length);
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
          metrics: [{ group_id: "provider_model:opaque", metric: "judge.safety.pass", alert: true,
            reference_value: 0.9, current_value: 0.6, effect: -0.3,
            p_adjusted: 0.01, reference_n: 80, current_n: 20 }],
          metric_coverage: [], groups: [{
            group_id: "provider_model:opaque", label: "openai / gpt-4.1",
            reference_units: 80, current_units: 20,
          }],
        },
      },
    },
  };

  const text = textOf(render(ui.Overview, createHooks(), { data }));
  assert.match(text, /historical comparison found 1 alert/i);
  assert.doesNotMatch(text, /safety pass rate/i);
  assert.doesNotMatch(text, /openai \/ gpt-4\.1/i);
  assert.doesNotMatch(text, /provider_model:opaque/i);
  assert.doesNotMatch(text, /No completed run/i);
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
  for (const [section, traceId, evaluatorId] of [
    ["history", "candidate-trace", "judge-b-id"],
    ["status", "active-trace", "judge-a-id"],
  ]) {
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
  assert.match(text, /Active monitor found 1 alert/i);
  assert.match(text, /HISTORICAL CHECK/);
  assert.match(text, /Historical comparison found no alert/i);
  assert.doesNotMatch(text, /active quality pass rate|candidate quality pass rate/i);
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
    const tree = render(ui.Dashboard, createHooks(), { data, source: "live" });
    assert.match(textOf(tree), /Synthetic local demo/);
    assert.match(textOf(tree), /Wholly synthetic demo data/);
  } finally { delete globalThis.window; }
});

async function resolveJson(request, payload) {
  request.resolve({ ok: true, json: async () => payload });
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

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

  const rendered = textOf(render(ui.Overview, createHooks(), { data }));

  assert.doesNotMatch(rendered, /Not configured/);
  assert.doesNotMatch(rendered, /No dimensions currently clear/);
});

test("judge scores directs an empty store to Evaluator Lab without legacy windows", async () => {
  const ui = await loadUiModule();
  const data = bundle("evaluator-a");

  const rendered = textOf(render(ui.Judge, createHooks(), { data })).replace(/\s+/g, " ");

  assert.match(rendered, /No Trace evaluator results have been stored yet/);
  assert.match(rendered, /Evaluator Lab stores results for an eligible trace set/);
  assert.doesNotMatch(rendered, /global content-bearing traces/);
});

test("unresolved evaluator selection affects judge results but not monitoring", async () => {
  const ui = await loadUiModule();
  const data = bundle(null);
  data.evaluation.status = "selection_required";

  const overviewTree = render(ui.Overview, createHooks(), { data });
  const overview = textOf(overviewTree);
  const judge = textOf(render(ui.Judge, createHooks(), { data }));
  const signalMetric = findAll(overviewTree,
    (node) => typeof node.type === "function" && node.type.name === "MetricCell")
    .find((node) => node.props.label === "Evaluation drift signals");

  assert.equal(signalMetric, undefined);
  assert.doesNotMatch(overview, /Evaluation drift signals/);
  assert.match(judge, /Select a Trace evaluator to view judge results/);
  assert.doesNotMatch(judge, /No Trace evaluator results have been stored yet/);
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
  assert.notEqual(dashboard.props.source, "live");
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
      summary: { stored: 120 }, analysis: { status: "completed" } } });
    await capturing;
    assert.equal(status(), "");
    assert.deepEqual(completed, ["local"]);
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
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
  const props = { url: "/api/runs", focusRunIds: ["run-a"], selectedRunId: "run-a",
    findingCode: "test_failed" };
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  await resolveJson(requests[0], { ...runListPage(0, ["run-a"], 1), runs: [run] });
  render(ui.Runs, hooks, props);
  hooks.flushEffects();
  const request = requests.find((item) => item.url.includes("event_id=event-failure"));
  assert.ok(request, "the linked event must load even when it is outside the first page");
  await resolveJson(request, { runId: "run-a", turns: [], events: [{ eventId: "event-failure",
    timelineIndex: 3, type: "test_result", status: "failed", occurredAt: "2026-09-01T00:00:00Z",
    attributes: { failed: 1 }, provenance: "synthetic:fixture", privacy: "metadata" }],
    page: { available: 1, shown: 1, offset: 0, limit: 100, truncated: false } });
  const tree = render(ui.Runs, hooks, props);
  const detail = findAll(tree, (node) => node.type?.name === "RunDetail")[0];
  assert.equal(detail.props.prioritizeDetail, true);
  const rendered = render(detail.type, createHooks(), detail.props);
  assert.match(rendered.props.className, /order-1 lg:order-2/);
  const content = textOf(rendered);
  assert.match(content, /Investigating: Test failed/);
  assert.match(content, /Run outcome:\s+completed/);
  assert.doesNotMatch(content, /Run status unavailable/);
  assert.match(content, /1 test execution reported failure/);
  assert.doesNotMatch(content, /test execution\(s\)/);
  assert.ok(content.indexOf("1 test execution reported failure") < content.indexOf("Inspect and rerun the test."));
  assert.ok(content.indexOf("Inspect and rerun the test.") < content.indexOf("Technical coverage and further evaluation"));
  assert.match(content, /Recorded evidence/);
  const evidence = findAll(rendered, (node) => node.type?.name === "EventRow" && node.props.focused)[0];
  assert.equal(evidence.props.event.eventId, "event-failure");
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
