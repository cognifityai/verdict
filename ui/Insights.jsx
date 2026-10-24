import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { agentEvidenceValue, agentTurnListPriceValue, datasetActivitySummary, datasetEvidenceRows, failureCountValue, sourceListPriceValue, sourceTokenValue } from "./insights-data.mjs";
import { analysisNeedsRefresh, isEvidenceGap } from "./guided-flow.mjs";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };

const SEVERITY_ORDER = { error: 0, warning: 1, info: 2 };
const FINDING_TITLES = {
  tool_error: "Tool failures recorded",
  command_failed: "Commands failed",
  test_failed: "Test failures recorded",
  business_outcome_failed: "Reported business outcomes failed",
  possible_tool_loop: "Repeated identical tool calls",
  required_step_missing: "Required evidence missing",
  prohibited_tool_used: "Prohibited tool used",
  response_schema_invalid: "Responses failed the JSON requirement",
  run_status_unknown: "Run outcome unavailable",
  response_not_evaluable: "Response unavailable for evaluation",
  event_capture_partial: "Event capture reached its limit",
  tool_outcomes_incomplete: "Tool outcome records incomplete",
};

export function Insights({ url, onOpenRuns, onAnalysisComplete, onData = null, currentMeta = null, mode = "findings" }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [running, setRunning] = useState(false);
  const load = React.useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      setState({ loading: false, error: null, data });
      onData?.(data, null);
    } catch (error) {
      setState({ loading: false, error: String(error), data: null });
      onData?.(null, String(error));
    }
  }, [url, onData]);
  useEffect(() => { void load(); }, [load]);
  const runAnalysis = React.useCallback(async () => {
    setRunning(true);
    setState((current) => ({ ...current, error: null }));
    try {
      const root = url.replace(/\/api\/insights(?:\?.*)?$/, "");
      const tokenResponse = await fetch(`${root}/api/setup/token`, { credentials: "same-origin" });
      if (!tokenResponse.ok) throw new Error(`HTTP ${tokenResponse.status}`);
      const { setupToken } = await tokenResponse.json();
      const response = await fetch(`${root}/api/insights/run`, {
        method: "POST", credentials: "same-origin",
        headers: { Accept: "application/json", "X-Verdict-Setup": setupToken },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await response.json();
      await load();
      await onAnalysisComplete?.();
    } catch (error) {
      setState((current) => ({ ...current, error: String(error) }));
    } finally { setRunning(false); }
  }, [load, onAnalysisComplete, url]);
  if (state.error) return <Notice icon={AlertTriangle}>Insights unavailable: {state.error}</Notice>;
  if (!state.data) return <Notice icon={RefreshCw}>Loading recorded findings…</Notice>;
  const data = state.data;
  const analysisState = data.analysisState || { status: "never_run" };
  const staleAnalysis = analysisNeedsRefresh(currentMeta, data);
  const counts = data.dataHealth.counts;
  const capturedRuns = Number.isInteger(currentMeta?.totalAgentRuns)
    ? currentMeta.totalAgentRuns : counts.runs;
  const traceEvidence = data.dataHealth.traceEvidence || { judgeEligible: 0, notEvaluable: 0 };
  const activity = datasetActivitySummary(data.dataHealth, data.performance);
  const findings = data.findings || [];
  const issues = findings.filter((finding) => !isEvidenceGap(finding))
    .sort((left, right) => (SEVERITY_ORDER[left.severity] ?? 3) - (SEVERITY_ORDER[right.severity] ?? 3)
      || (right.runs || 0) - (left.runs || 0) || left.code.localeCompare(right.code));
  const evidenceGaps = findings.filter(isEvidenceGap);
  const largestIssueCount = Math.max(1, ...issues.map((finding) => finding.runs || 0));
  if (mode === "reliability") return <ProductView title="Reliability" intro="Deterministic execution outcomes from captured evidence; no judge is required." rows={[
    ["LLM trace evidence", traceOutcomeSummary(data.reliability.traceOutcomes)],
    ["Agent Run outcomes (complete histories)", counts.runs ? displayCounts(data.reliability.runOutcomes) : "Not available — no Agent Runs captured"],
    ["Observed turn outcomes (partial histories may be included)", counts.turns ? displayCounts(data.reliability.turnOutcomes) : "Not available — no Agent Runs captured"],
    ["Tool outcome records", toolRecordSummary(data.performance.toolCalls ?? 0, data.reliability.toolOutcomesReported ?? 0)],
    ["Tool errors among reported results", agentEvidenceValue(data.dataHealth, failureCountValue(data.reliability.toolErrors, data.reliability.toolOutcomesReported))],
    ["Command failures", agentEvidenceValue(data.dataHealth, failureCountValue(data.reliability.commandFailures, data.reliability.commandOutcomesReported))],
    ["Test failures", agentEvidenceValue(data.dataHealth, failureCountValue(data.reliability.testFailures ?? 0, data.reliability.testOutcomesReported))],
    ["Judge-eligible traces", traceEvidence.judgeEligible],
    ["Traces without judge evidence", traceEvidence.notEvaluable],
  ]} comparisons={data.modelComparisons} scope={data.scope} />;
  if (mode === "performance") return <ProductView title="Performance" intro="Token, latency and priced-cost coverage for genuine model-call traces." rows={[
    ["Model calls", data.performance.modelCalls], ["Input tokens", data.performance.inputTokens ?? "Not captured"],
    ["Output tokens", data.performance.outputTokens ?? "Not captured"], ["Average latency", data.performance.averageModelLatencyMs == null ? "Not captured" : `${data.performance.averageModelLatencyMs} ms`],
    ["Known latency calls", data.performance.latencyKnownCalls], ["Cost", data.performance.costUsd == null ? "Not captured" : `$${data.performance.costUsd}`],
    ["Estimated list price (agent turns)", agentTurnListPriceValue(data.performance)],
  ]} comparisons={data.modelComparisons} scope={data.scope} />;
  if (mode === "behavior") return <ProductView title="Behavior" intro="Transparent structural response indicators. These are signatures, not semantic quality judgments." rows={[
    ["Captured responses", data.behavior.capturedResponses], ["Average response characters", data.behavior.averageResponseCharacters ?? "Not captured"],
    ["Refusal signatures", data.behavior.refusals], ["Apology starts", data.behavior.apologyStarts],
    ["Hedge phrases", data.behavior.hedges], ["Valid JSON responses", data.behavior.validJsonResponses],
  ]} comparisons={data.modelComparisons} scope={data.scope} />;
  return <div className="space-y-5">
    {analysisState.status === "never_run" && <section className="border p-5" style={{ borderColor: C.amber, background: C.panel }}>
      <div className="font-semibold">Initial analysis has not run</div>
      <p className="text-sm mt-2" style={{ color: C.sub }}>{capturedRuns > 0 ? "Captured activity is ready for local analysis. Run it to inspect recorded issues." : "No deterministic analysis snapshot has been published yet. Capture or import evidence, then run local analysis."}</p>
      <button disabled={running} onClick={runAnalysis} className="mt-4 border px-4 py-2 text-sm" style={{ borderColor: C.green, color: C.green }}>{running ? "Analyzing…" : "Run initial analysis"}</button>
    </section>}
    {analysisState.status === "error" && <Notice icon={AlertTriangle} color={C.red}>The latest analysis failed. No empty or zero-finding result is being inferred. <button disabled={running} onClick={runAnalysis} className="underline ml-2">Retry</button></Notice>}
    {staleAnalysis && <Notice icon={AlertTriangle} color={C.amber}>Saved findings need a fresh local check before you rely on them.</Notice>}
    {!data.scope.complete && <Notice icon={AlertTriangle} color={C.amber}>This view is partial: {data.scope.analyzedRuns} of {data.scope.availableRuns} runs were analyzed; {data.scope.partialRuns || 0} histories contain only a recorded prefix. Those activity counts are lower bounds; whole-run outcomes are excluded.</Notice>}
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>{mode !== "home" && <><div className="text-xs font-mono" style={{ color: C.green }}>RECORDED EVIDENCE</div><h2 className="text-lg font-semibold mt-1">What needs attention</h2></>}
          <p className="text-sm mt-2" style={{ color: C.sub }}>{analysisState.status === "completed"
            ? `${issues.length} recorded issue ${issues.length === 1 ? "type" : "types"} across ${data.scope.analyzedRuns.toLocaleString()} of ${data.scope.availableRuns.toLocaleString()} runs in this analysis snapshot. ${data.scope.partialRuns ? `${data.scope.partialRuns} run histories contain only a recorded prefix. ` : ""}${issues.length ? "Select one to inspect what happened and what to do next." : evidenceGaps.length || data.scope.partialRuns ? "Review incomplete evidence before drawing conclusions." : "No recorded issue needs inspection in this captured scope."}`
            : `${capturedRuns.toLocaleString()} captured run${capturedRuns === 1 ? "" : "s"}. Run local analysis to find recorded issues.`}</p>
        </div>
        {analysisState.status === "completed" && <button disabled={running} onClick={runAnalysis} className="border px-3 py-2 text-xs" style={{ borderColor: C.green, color: C.green }}>{running ? "Analyzing…" : "Refresh local analysis"}</button>}
      </div>
      {issues.length ? <div role="group" aria-label="Recorded issues by affected runs" className="mt-5 space-y-3">
        {issues.slice(0, 5).map((finding) => FindingBar({ finding, maximum: largestIssueCount, onOpen: () => onOpenRuns?.(finding) }))}
        {issues.length > 5 && <details className="border-t pt-3" style={{ borderColor: C.border }}>
          <summary className="cursor-pointer text-sm">Show {issues.length - 5} more issue types</summary>
          <div className="mt-3 space-y-3">{issues.slice(5).map((finding) => FindingBar({ finding, maximum: largestIssueCount, onOpen: () => onOpenRuns?.(finding) }))}</div>
        </details>}
        <p className="text-xs" style={{ color: C.faint }}>A run may have more than one issue. Bars compare affected-run counts within this analysis.</p>
      </div> : <div className="flex items-center gap-2 text-sm mt-5" style={{ color: C.sub }}>{analysisState.status === "completed" ? evidenceGaps.length || data.scope.partialRuns ? <><AlertTriangle size={15} style={{ color: C.amber }} />No recorded issues in captured evidence. Some histories are partial; review evidence coverage below.</> : <><CheckCircle2 size={15} style={{ color: C.green }} />No deterministic issues found in the analyzed snapshot.</> : "No analyzed findings are available yet."}</div>}
      {evidenceGaps.length > 0 && <details open={mode === "home" && issues.length === 0} className="mt-5 border-t pt-4" style={{ borderColor: C.border }}>
        <summary id={mode === "home" ? "guided-evidence-gaps" : undefined} className="cursor-pointer text-sm font-semibold">Evidence gaps ({evidenceGaps.length} {evidenceGaps.length === 1 ? "type" : "types"})</summary>
        <p className="text-sm mt-2" style={{ color: C.sub }}>These limit what Verdict can conclude from captured records. They are not confirmed task failures.</p>
        <div className="mt-3 space-y-2">{evidenceGaps.map((finding) => <button key={finding.code} onClick={() => onOpenRuns?.(finding)} className="w-full border p-3 text-left" style={{ borderColor: C.border }}>
          <span className="text-sm font-semibold" style={{ color: C.sub }}>{FINDING_TITLES[finding.code] || finding.code.replaceAll("_", " ")} · {finding.runs} run{finding.runs === 1 ? "" : "s"}</span>
          <div className="text-sm mt-1">{finding.message}</div>
          <div className="text-xs mt-2" style={{ color: C.green }}>Inspect affected runs →</div>
        </button>)}</div>
      </details>}
    </section>
    {analysisState.status !== "never_run" && <details className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <summary className="cursor-pointer font-semibold">Activity and evidence breakdown</summary>
      <div className="mt-5 space-y-5">
    {analysisState.status === "completed" && <div className="text-xs font-mono" style={{ color: C.faint }}>Analysis completed {analysisState.completedAt} · evidence cutoff {analysisState.cutoff}</div>}
    <section className="grid sm:grid-cols-3 gap-3">
      <Metric label="Agent runs" value={counts.runs} sub={`${counts.turns} observed turns${data.scope.partialRuns ? " (partial histories included)" : ""}`} />
      <Metric label={activity.activityLabel} value={activity.activityValue} sub={activity.activityDetail} />
      <Metric label={activity.linkLabel} value={activity.linkValue} sub={activity.linkDetail} />
    </section>
    <div className="grid lg:grid-cols-3 gap-4">
      <Section title="Reliability" rows={[
        ["Tool call / reported result records", `${data.performance.toolCalls ?? 0} / ${data.reliability.toolOutcomesReported ?? 0}`],
        ["Tool errors among reported results", agentEvidenceValue(data.dataHealth, failureCountValue(data.reliability.toolErrors, data.reliability.toolOutcomesReported))],
        ["Command failures", agentEvidenceValue(data.dataHealth, failureCountValue(data.reliability.commandFailures, data.reliability.commandOutcomesReported))],
        ["Test failures", agentEvidenceValue(data.dataHealth, failureCountValue(data.reliability.testFailures ?? 0, data.reliability.testOutcomesReported))],
        ["Run outcomes (complete histories)", agentEvidenceValue(data.dataHealth, displayCounts(data.reliability.runOutcomes))],
        ["Observed turn outcomes (partial histories may be included)", agentEvidenceValue(data.dataHealth, displayCounts(data.reliability.turnOutcomes))],
      ]} />
      <Section title="Performance" rows={[
        ["Trace input tokens", data.performance.inputTokens ?? "Not captured"], ["Trace output tokens", data.performance.outputTokens ?? "Not captured"],
        ["Average model latency", data.performance.averageModelLatencyMs == null ? "Not captured" : `${data.performance.averageModelLatencyMs} ms`],
        ["Cost", data.performance.costState === "not_captured" ? "Not captured" : data.performance.costUsd],
        ["Estimated list price (agent turns)", agentEvidenceValue(data.dataHealth, agentTurnListPriceValue(data.performance))],
      ]} />
      <Section title="Evidence health" rows={[
        ...datasetEvidenceRows(data.dataHealth, data.scope),
        ["Observed event statuses (partial histories may be included)", agentEvidenceValue(data.dataHealth, displayCounts(data.dataHealth.eventStatuses))],
        ["Observed event types (partial histories may be included)", agentEvidenceValue(data.dataHealth, Object.keys(data.dataHealth.eventTypes).length)],
      ]} />
    </div>
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">Source evidence coverage and activity</h2>
      <p className="text-sm mt-2" style={{ color: C.sub }}>Observed activity is not a task-quality ranking. Partial source histories contribute recorded prefix counts only; whole-run outcomes are excluded. Provider-call latency, price, judging, and model comparisons use genuine LLM Traces above.</p>
      <div className="overflow-x-auto mt-3"><table className="w-full text-sm"><thead><tr style={{ color: C.faint }}><th className="text-left p-2">Source</th><th className="text-right p-2">Runs / children</th><th className="text-right p-2">Turns / final responses</th><th className="text-right p-2">Tool calls / reported results</th><th className="text-right p-2">Tool / command / test failures</th><th className="text-right p-2">Source-reported tokens</th><th className="text-right p-2">Est. list price</th><th className="text-right p-2">Token coverage</th><th className="text-right p-2">Run outcomes</th></tr></thead><tbody>{(data.sourceActivity || []).map((row) => <tr key={row.source} className="border-t" style={{ borderColor: C.border }}><td className="p-2">{row.source}{row.partialRuns ? <span className="block text-xs" style={{ color: C.amber }}>{row.partialRuns} partial histories</span> : null}</td><td className="text-right p-2">{row.runs} / {row.childRuns}</td><td className="text-right p-2">{row.turns} / {row.finalResponses}{row.truncatedResponses ? ` (${row.truncatedResponses} previews)` : ""}</td><td className="text-right p-2">{row.toolCalls} / {row.toolOutcomesReported}</td><td className="text-right p-2">{failureCountValue(row.toolErrors, row.toolOutcomesReported, "—")} / {failureCountValue(row.commandFailures, row.commandOutcomesReported, "—")} / {failureCountValue(row.testFailures, row.testOutcomesReported, "—")}</td><td className="text-right p-2">{sourceTokenValue(row)}</td><td className="text-right p-2">{sourceListPriceValue(row)}</td><td className="text-right p-2">{row.tokenUsageTurns}/{row.turns} turns</td><td className="text-right p-2">{displayCounts(row.runOutcomes)}</td></tr>)}</tbody></table></div>
      <p className="text-xs mt-2" style={{ color: C.faint }}>Tool calls and reported results are separate event-record counts; they do not establish one-to-one pairing. — means the source does not report success or failure for that event type, so no failure count is shown. The list price is an estimate from token counts and public per-token rates, not a bill.</p>
    </section>
      </div>
    </details>}
  </div>;
}

function displayCounts(values) { return Object.entries(values || {}).map(([name, count]) => `${name}: ${count}`).join(" · ") || "None"; }

function traceOutcomeSummary(values) {
  const labels = {
    error_recorded: "Provider error recorded",
    ended_no_error_recorded: "Ended without recorded provider error",
    unfinished: "Unfinished",
    // Older saved snapshots remain readable without calling absence of error success.
    failed: "Provider error recorded",
    succeeded: "No provider error recorded (completion unverified)",
    no_error_recorded: "No provider error recorded (completion unverified)",
  };
  return Object.entries(values || {}).map(([name, count]) =>
    `${labels[name] || name}: ${count}`).join(" · ") || "None";
}
function toolRecordSummary(calls, results) { return `${calls} tool call ${calls === 1 ? "record" : "records"} · ${results} reported tool ${results === 1 ? "result" : "results"}`; }
function FindingBar({ finding, maximum, onOpen }) {
  const color = finding.severity === "error" ? C.red : finding.severity === "warning" ? C.amber : C.green;
  const width = Math.min(100, Math.max(0, (finding.runs || 0) / maximum * 100));
  return <button key={finding.code} type="button" onClick={onOpen} className="w-full border p-3 text-left" style={{ borderColor: C.border }}>
    <span className="flex justify-between gap-2 text-sm font-semibold"><span style={{ color }}>{FINDING_TITLES[finding.code] || finding.code.replaceAll("_", " ")}</span><span className="whitespace-nowrap">{finding.runs} run{finding.runs === 1 ? "" : "s"}</span></span>
    <span className="block h-2 mt-2" style={{ background: C.border }}><span className="block h-full" style={{ width: `${width}%`, background: color }} /></span>
    {finding.partialRuns > 0 && <span className="block text-xs mt-2" style={{ color: C.amber }}>{finding.partialRuns} affected run{finding.partialRuns === 1 ? "" : "s"} from a recorded historical prefix</span>}
    <span className="block text-xs mt-2" style={{ color: C.green }}>Inspect affected runs →</span>
  </button>;
}
function Metric({ label, value, sub }) { return <div className="border p-4" style={{ borderColor: C.border, background: C.panel }}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-2xl font-semibold mt-1">{value}</div><div className="text-xs mt-1" style={{ color: C.sub }}>{sub}</div></div>; }
function Section({ title, rows }) { return <section className="border p-4" style={{ borderColor: C.border, background: C.panel }}><h2 className="font-semibold">{title}</h2><div className="mt-3 space-y-3">{rows.map(([label, value]) => <div key={label}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-sm mt-1 break-words">{value}</div></div>)}</div></section>; }
function Notice({ icon: Icon, children, color = C.sub }) { return <div className="border p-4 flex items-center gap-2 text-sm" style={{ borderColor: C.border, background: C.panel, color }}><Icon size={15} />{children}</div>; }
function traceTokenValue(row) { if (row.inputTokens == null && row.outputTokens == null) return "Not captured"; const total = (row.inputTokens || 0) + (row.outputTokens || 0); return `${total.toLocaleString()}${row.inputTokens == null || row.outputTokens == null ? " (partial)" : ""}`; }
function ProductView({ title, intro, rows, comparisons, scope }) { return <div className="space-y-5">{scope?.complete === false && <p role="status" className="border p-3 text-sm" style={{ borderColor: C.amber, color: C.amber }}>Agent Run scan is partial: {scope.analyzedRuns} of {scope.availableRuns} runs analyzed; {scope.partialRuns || 0} histories contain a recorded prefix. Activity counts may be lower bounds and whole-run outcomes exclude those histories.</p>}{scope?.traces?.complete === false && <p role="status" className="border p-3 text-sm" style={{ borderColor: C.amber, color: C.amber }}>Trace scan is partial: {scope.traces.analyzed} of {scope.traces.available} traces. Trace totals and comparisons may be incomplete.</p>}<section className="border p-5" style={{ borderColor: C.border, background: C.panel }}><div className="text-xs font-mono" style={{ color: C.green }}>JUDGE-FREE ANALYSIS</div><h2 className="text-lg font-semibold mt-1">{title}</h2><p className="text-sm mt-2" style={{ color: C.sub }}>{intro}</p><div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-5">{rows.map(([label, value]) => <Metric key={label} label={label} value={value} sub="bounded captured-evidence scan" />)}</div></section><section className="border p-5 overflow-x-auto" style={{ borderColor: C.border, background: C.panel }}><h2 className="font-semibold">Provider and model comparison</h2><table className="w-full text-sm mt-3"><thead><tr style={{ color: C.faint }}><th className="text-left p-2">Provider / model</th><th className="text-right p-2">Traces</th><th className="text-right p-2">Errors</th><th className="text-right p-2">Tokens</th><th className="text-right p-2">Latency</th><th className="text-right p-2">Cost</th></tr></thead><tbody>{(comparisons || []).map((row) => <tr key={`${row.provider}:${row.model}`} className="border-t" style={{ borderColor: C.border }}><td className="p-2">{row.provider} · {row.model}</td><td className="text-right p-2">{row.traces}</td><td className="text-right p-2">{row.errors}</td><td className="text-right p-2">{traceTokenValue(row)}</td><td className="text-right p-2">{row.averageLatencyMs == null ? "—" : `${row.averageLatencyMs} ms`}</td><td className="text-right p-2">{row.costUsd == null ? "—" : `$${row.costUsd}`}</td></tr>)}</tbody></table></section></div>; }
