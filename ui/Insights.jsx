import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };

export function Insights({ url, onOpenRuns, mode = "findings" }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [running, setRunning] = useState(false);
  const load = React.useCallback(() => {
    setState((current) => ({ ...current, loading: true, error: null }));
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((error) => setState({ loading: false, error: String(error), data: null }));
  }, [url]);
  useEffect(load, [load]);
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
      setState({ loading: false, error: null, data: await response.json() });
    } catch (error) {
      setState((current) => ({ ...current, error: String(error) }));
    } finally { setRunning(false); }
  }, [url]);
  if (state.error) return <Notice icon={AlertTriangle}>Insights unavailable: {state.error}</Notice>;
  if (!state.data) return <Notice icon={RefreshCw}>Analyzing captured evidence…</Notice>;
  const data = state.data;
  const analysisState = data.analysisState || { status: "never_run" };
  const counts = data.dataHealth.counts;
  const traceEvidence = data.dataHealth.traceEvidence || { judgeEligible: 0, notEvaluable: 0 };
  if (mode === "reliability") return <ProductView title="Reliability" intro="Deterministic execution outcomes from captured evidence; no judge is required." rows={[
    ["LLM trace outcomes", displayCounts(data.reliability.traceOutcomes)],
    ["Agent Run outcomes", counts.runs ? displayCounts(data.reliability.runOutcomes) : "Not available — no Agent Runs captured"],
    ["Agent turn outcomes", counts.turns ? displayCounts(data.reliability.turnOutcomes) : "Not available — no Agent Runs captured"],
    ["Tool errors", data.reliability.toolErrors], ["Command failures", data.reliability.commandFailures],
    ["Judge-eligible traces", traceEvidence.judgeEligible],
    ["Traces without judge evidence", traceEvidence.notEvaluable],
  ]} comparisons={data.modelComparisons} />;
  if (mode === "performance") return <ProductView title="Performance" intro="Token, latency and priced-cost coverage for genuine model-call traces." rows={[
    ["Model calls", data.performance.modelCalls], ["Input tokens", data.performance.inputTokens],
    ["Output tokens", data.performance.outputTokens], ["Average latency", data.performance.averageModelLatencyMs == null ? "Not captured" : `${data.performance.averageModelLatencyMs} ms`],
    ["Known latency calls", data.performance.latencyKnownCalls], ["Cost", data.performance.costUsd == null ? "Not captured" : `$${data.performance.costUsd}`],
  ]} comparisons={data.modelComparisons} />;
  if (mode === "behavior") return <ProductView title="Behavior" intro="Transparent structural response indicators. These are signatures, not semantic quality judgments." rows={[
    ["Captured responses", data.behavior.capturedResponses], ["Average response characters", data.behavior.averageResponseCharacters ?? "Not captured"],
    ["Refusal signatures", data.behavior.refusals], ["Apology starts", data.behavior.apologyStarts],
    ["Hedge phrases", data.behavior.hedges], ["Valid JSON responses", data.behavior.validJsonResponses],
  ]} comparisons={data.modelComparisons} />;
  return <div className="space-y-5">
    {analysisState.status === "never_run" && <section className="border p-5" style={{ borderColor: C.amber, background: C.panel }}>
      <div className="font-semibold">Initial analysis has not run</div>
      <p className="text-sm mt-2" style={{ color: C.sub }}>Captured evidence is stored, but Verdict has not published a deterministic analysis snapshot for it yet.</p>
      <button disabled={running} onClick={runAnalysis} className="mt-4 border px-4 py-2 text-sm" style={{ borderColor: C.green, color: C.green }}>{running ? "Analyzing…" : "Run initial analysis"}</button>
    </section>}
    {analysisState.status === "error" && <Notice icon={AlertTriangle} color={C.red}>The latest analysis failed. No empty or zero-finding result is being inferred. <button disabled={running} onClick={runAnalysis} className="underline ml-2">Retry</button></Notice>}
    {analysisState.status === "completed" && <div className="text-xs font-mono" style={{ color: C.faint }}>Analysis completed {analysisState.completedAt} · evidence cutoff {analysisState.cutoff}</div>}
    {!data.scope.complete && <Notice icon={AlertTriangle} color={C.amber}>This view is partial: {data.scope.analyzedRuns} of {data.scope.availableRuns} runs were analyzed.</Notice>}
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><div className="text-xs font-mono" style={{ color: C.green }}>WHAT NEEDS ATTENTION</div><h2 className="text-lg font-semibold mt-1">Findings across captured evidence</h2></div>
        <div className="flex gap-2"><button onClick={load} className="border px-3 py-2 text-xs" style={{ borderColor: C.border }}><RefreshCw size={13} className="inline mr-2" />Refresh view</button><button disabled={running} onClick={runAnalysis} className="border px-3 py-2 text-xs" style={{ borderColor: C.green, color: C.green }}>{running ? "Analyzing…" : "Analyze latest evidence"}</button></div>
      </div>
      <div className="mt-4 space-y-2">
        {data.findings.length ? data.findings.map((finding) => <button key={finding.code} onClick={() => onOpenRuns?.(finding)} className="w-full border p-3 text-left" style={{ borderColor: finding.severity === "error" ? C.red : C.border }}>
          <span className="font-mono text-xs" style={{ color: finding.severity === "error" ? C.red : finding.severity === "warning" ? C.amber : C.sub }}>{finding.code} · {finding.runs} run{finding.runs === 1 ? "" : "s"}</span>
          <div className="text-sm mt-1">{finding.message}</div>
        </button>) : <div className="flex items-center gap-2 text-sm" style={{ color: C.sub }}><CheckCircle2 size={15} style={{ color: C.green }} />No deterministic findings in the analyzed evidence.</div>}
      </div>
    </section>
    <section className="grid sm:grid-cols-3 gap-3">
      <Metric label="Agent runs" value={counts.runs} sub={`${counts.turns} turns`} />
      <Metric label="Normalized events" value={counts.events} sub={`${data.performance.modelCalls} model · ${data.performance.toolCalls} tool calls`} />
      <Metric label="Model-call trace links" value={`${data.dataHealth.traceLinks.linked}/${data.dataHealth.traceLinks.modelCalls}`} sub={`${data.dataHealth.traceLinks.unlinked} unlinked`} />
    </section>
    <div className="grid lg:grid-cols-3 gap-4">
      <Section title="Reliability" rows={[
        ["Tool errors", data.reliability.toolErrors], ["Command failures", data.reliability.commandFailures],
        ["Run outcomes", displayCounts(data.reliability.runOutcomes)], ["Turn outcomes", displayCounts(data.reliability.turnOutcomes)],
      ]} />
      <Section title="Performance" rows={[
        ["Input tokens", data.performance.inputTokens], ["Output tokens", data.performance.outputTokens],
        ["Average model latency", data.performance.averageModelLatencyMs == null ? "Not captured" : `${data.performance.averageModelLatencyMs} ms`],
        ["Cost", data.performance.costState === "not_captured" ? "Not captured" : data.performance.costUsd],
      ]} />
      <Section title="Evidence health" rows={[
        ["Prompt evidence", displayCounts(data.dataHealth.promptStates)], ["Response evidence", displayCounts(data.dataHealth.responseStates)],
        ["Event statuses", displayCounts(data.dataHealth.eventStatuses)], ["Event types", Object.keys(data.dataHealth.eventTypes).length],
      ]} />
    </div>
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">Agent-run comparisons</h2>
      <div className="overflow-x-auto mt-3"><table className="w-full text-sm"><thead><tr style={{ color: C.faint }}><th className="text-left p-2">Source</th><th className="text-right p-2">Runs / outcomes</th><th className="text-right p-2">Model calls</th><th className="text-right p-2">Tool / command failures</th><th className="text-right p-2">Tokens</th><th className="text-right p-2">Latency</th><th className="text-right p-2">Retries</th><th className="text-right p-2">Cost</th></tr></thead><tbody>{data.comparisons.map((row) => <tr key={row.source} className="border-t" style={{ borderColor: C.border }}><td className="p-2">{row.source}</td><td className="text-right p-2">{row.runs} · {displayCounts(row.runOutcomes)}</td><td className="text-right p-2">{row.modelCalls}</td><td className="text-right p-2">{row.toolErrors} / {row.commandFailures}</td><td className="text-right p-2">{row.inputTokens + row.outputTokens}</td><td className="text-right p-2">{row.averageModelLatencyMs == null ? "Not captured" : `${row.averageModelLatencyMs} ms`}</td><td className="text-right p-2">{row.retryState === "not_captured" ? "Not captured" : row.retries}</td><td className="text-right p-2">{row.costUsd == null ? "Not captured" : `$${row.costUsd}`}{row.costState === "partial" ? " (partial)" : ""}</td></tr>)}</tbody></table></div>
    </section>
  </div>;
}

function displayCounts(values) { return Object.entries(values || {}).map(([name, count]) => `${name}: ${count}`).join(" · ") || "None"; }
function Metric({ label, value, sub }) { return <div className="border p-4" style={{ borderColor: C.border, background: C.panel }}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-2xl font-semibold mt-1">{value}</div><div className="text-xs mt-1" style={{ color: C.sub }}>{sub}</div></div>; }
function Section({ title, rows }) { return <section className="border p-4" style={{ borderColor: C.border, background: C.panel }}><h2 className="font-semibold">{title}</h2><div className="mt-3 space-y-3">{rows.map(([label, value]) => <div key={label}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-sm mt-1 break-words">{value}</div></div>)}</div></section>; }
function Notice({ icon: Icon, children, color = C.sub }) { return <div className="border p-4 flex items-center gap-2 text-sm" style={{ borderColor: C.border, background: C.panel, color }}><Icon size={15} />{children}</div>; }
function ProductView({ title, intro, rows, comparisons }) { return <div className="space-y-5"><section className="border p-5" style={{ borderColor: C.border, background: C.panel }}><div className="text-xs font-mono" style={{ color: C.green }}>JUDGE-FREE ANALYSIS</div><h2 className="text-lg font-semibold mt-1">{title}</h2><p className="text-sm mt-2" style={{ color: C.sub }}>{intro}</p><div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-5">{rows.map(([label, value]) => <Metric key={label} label={label} value={value} sub="dataset-wide bounded scan" />)}</div></section><section className="border p-5 overflow-x-auto" style={{ borderColor: C.border, background: C.panel }}><h2 className="font-semibold">Provider and model comparison</h2><table className="w-full text-sm mt-3"><thead><tr style={{ color: C.faint }}><th className="text-left p-2">Provider / model</th><th className="text-right p-2">Traces</th><th className="text-right p-2">Errors</th><th className="text-right p-2">Tokens</th><th className="text-right p-2">Latency</th><th className="text-right p-2">Cost</th></tr></thead><tbody>{(comparisons || []).map((row) => <tr key={`${row.provider}:${row.model}`} className="border-t" style={{ borderColor: C.border }}><td className="p-2">{row.provider} · {row.model}</td><td className="text-right p-2">{row.traces}</td><td className="text-right p-2">{row.errors}</td><td className="text-right p-2">{row.inputTokens + row.outputTokens}</td><td className="text-right p-2">{row.averageLatencyMs == null ? "—" : `${row.averageLatencyMs} ms`}</td><td className="text-right p-2">{row.costUsd == null ? "—" : `$${row.costUsd}`}</td></tr>)}</tbody></table></section></div>; }
