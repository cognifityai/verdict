import React, { useEffect, useState, useRef } from "react";
import { monitorRequest } from "./monitor-form.mjs";

const box = { borderColor: "#26332e", background: "#111715" };

export function monitorStateParts(monitor) {
  const legacy = monitor?.policy && monitor?.snapshot ? monitor : null;
  return {
    active: monitor?.active || (["active", "requires_rebootstrap"].includes(monitor?.state) ? legacy : null),
    candidate: monitor?.candidate || (monitor?.state === "candidate" ? legacy : null),
  };
}

export const metricLabel = (metric) => {
  if (metric.startsWith("score.")) { const name=metric.slice(6).replaceAll("_"," "); return name.endsWith("score") ? name : `${name} score`; }
  if (metric.startsWith("judge.") && metric.endsWith(".pass")) {
    return `${metric.slice(6, -5).replaceAll("_", " ")} pass rate`;
  }
  return ({
    provider_error: "Provider error rate",
    response_empty: "Empty-response rate",
    refusal_signature: "Refusal-language rate",
    "session.completed": "Recorded completion rate",
    "agent.execution_completed": "Completed-execution rate",
    "agent.final_output_present": "Final-output presence rate",
  })[metric] || metric.replaceAll("_", " ");
};

export function LogicalSessionPreview({ preview }) {
  return <section className="border p-5" style={box}>
    <div className="flex flex-wrap gap-3 items-center justify-between">
      <div><div className="text-xs font-mono" style={{ color: "#f2b84b" }}>DESCRIPTIVE LOGICAL-SESSION COMPARISON</div><div className="font-semibold mt-1">Observational rates only</div></div>
      <div className="text-sm" style={{ color: "#94a39d" }}>{preview.reference.unitCount} reference → {preview.current.unitCount} current logical sessions</div>
    </div>
    <p className="text-sm mt-3" style={{ color: "#94a39d" }}>Verdict has not established independent sampling or authoritative session finalization. This as-of preview has no p-values, alert decision, or activation path.</p>
    <div className="mt-4 space-y-2">{preview.metrics.map((metric) => <div key={metric.metric} className="border p-3 text-sm" style={{ borderColor: "#26332e" }}>
      <span className="font-mono">{metricLabel(metric.metric)}</span>
      {metric.referenceValue == null || metric.currentValue == null
        ? <span className="ml-3" style={{ color: "#f2b84b" }}>No complete rate comparison</span>
        : <span className="ml-3" style={{ color: "#94a39d" }}>{(100 * metric.referenceValue).toFixed(1)}% → {(100 * metric.currentValue).toFixed(1)}% · difference {(100 * metric.effect).toFixed(1)}pp · observational n {metric.referenceEvaluable} → {metric.currentEvaluable}</span>}
      <div className="text-xs mt-2" style={{ color: "#94a39d" }}>Evidence: {metric.referenceEvaluable} → {metric.currentEvaluable} evaluable · {metric.referenceUnclear} → {metric.currentUnclear} unclear · {metric.referenceMissing} → {metric.currentMissing} missing · {metric.referenceError} → {metric.currentError} evaluator errors</div>
    </div>)}</div>
    {(preview.coverage.runsMissingLogicalSession > 0 || preview.coverage.sessionsInProgress > 0) && <p className="text-xs mt-4" style={{ color: "#f2b84b" }}>Coverage: {preview.coverage.runsMissingLogicalSession} runs lacked logical-session identity; {preview.coverage.sessionsInProgress} sessions were still in progress. Neither was guessed into a cohort.</p>}
  </section>;
}

export function HistoricalObservations({preview}) {
  const coverage=new Map(preview.metricCoverage.map(r=>[`${r.group_id || "all"}:${r.metric}`,r]));
  return <section className="border p-5 space-y-3" style={box}><h2 className="font-semibold">Observed changes · exploratory</h2><p className="text-sm" style={{color:"#f2b84b"}}>{preview.repair} No monitor was created or activated.</p><p className="text-sm">{preview.populationCounts.reference} base → {preview.populationCounts.current} current observations · pending {preview.pendingCounts.reference} → {preview.pendingCounts.current}</p>{preview.earlyIndicators.map(r=>{const c=coverage.get(`${r.group_id || "all"}:${r.metric}`);return <div key={`${r.group_id || "all"}:${r.metric}`} className="border p-3 text-sm" style={box}>{metricLabel(r.metric)} · {r.reference_value == null ? "Unavailable" : r.kind === "number" ? r.reference_value.toFixed(1) : `${(100*r.reference_value).toFixed(1)}%`} → {r.current_value == null ? "Unavailable" : r.kind === "number" ? r.current_value.toFixed(1) : `${(100*r.current_value).toFixed(1)}%`} · evaluable n {r.reference_n} → {r.current_n}{r.reference_ci && r.current_ci && <div className="text-xs mt-1" style={{color:"#94a39d"}}>95% rate intervals {(100*r.reference_ci[0]).toFixed(1)}–{(100*r.reference_ci[1]).toFixed(1)}% → {(100*r.current_ci[0]).toFixed(1)}–{(100*r.current_ci[1]).toFixed(1)}%</div>}{c && <div className="text-xs mt-1" style={{color:"#94a39d"}}>Missing {c.reference_missing} → {c.current_missing} · unclear {c.reference_unclear} → {c.current_unclear} · judge errors {c.reference_error} → {c.current_error}</div>}</div>})}</section>;
}

export const canRunActiveMonitor = (active) => active?.state === "active";

export function AgentEvaluatorDiscoveryNote({ truncated }) {
  return truncated ? <span className="block text-xs mt-1" style={{ color: "#f2b84b" }}>Evaluator choices use the newest 1,000 stored Turn-result slots; older identities are not shown.</span> : null;
}

const pct = (value) => `${(100 * value).toFixed(1)}%`;

const pValue = (value) => {
  if (!Number.isFinite(value)) return "unavailable";
  if (value === 0) return "0";
  if (value < 0.001) return value.toExponential(2);
  return String(Number(value.toPrecision(3)));
};

const goodWhenHigh = (metric) => metric.startsWith("judge.")
  || ["agent.execution_completed", "agent.final_output_present", "session.completed"].includes(metric);

const changeDirection = (metric) => goodWhenHigh(metric) ? 1 : -1;

const shortId = (value) => value.length <= 20
  ? value
  : `${value.slice(0, 10)}…${value.slice(-6)}`;

function ComparisonBar({ label, value, color }) {
  return <div className="grid grid-cols-[72px_minmax(0,1fr)_56px] items-center gap-3 text-xs">
    <span style={{ color: "#94a39d" }}>{label}</span>
    <div className="h-3 overflow-hidden" style={{ background: "#26332e", borderRadius: 2 }}>
      <div style={{ width: `${Math.max(0, Math.min(100, 100 * value))}%`, height: "100%", background: color }} />
    </div>
    <span className="text-right font-mono">{pct(value)}</span>
  </div>;
}

function EvidenceLinks({ label, unitIds, onOpenTrace }) {
  if (!unitIds?.length) return null;
  return <div>
    <div className="text-xs" style={{ color: "#94a39d" }}>{label}</div>
    <div className="flex flex-wrap gap-2 mt-2">
      {unitIds.map((unitId) => <button type="button" key={unitId} title={unitId}
        onClick={onOpenTrace ? () => onOpenTrace(unitId) : undefined}
        disabled={!onOpenTrace} className="border px-2 py-1 text-xs font-mono disabled:cursor-default"
        style={{ color: "#4ee1aa", borderColor: "#26332e", borderRadius: 3 }}>
        {shortId(unitId)}
      </button>)}
    </div>
  </div>;
}

function MetricComparisonCard({
  metric, evidence, group, referenceEvidenceUnitIds, currentEvidenceUnitIds,
  onOpenTrace,
}) {
  const numeric = metric.kind === "number";
  const regression = numeric
    ? metric.movement === "deteriorated"
    : metric.effect * changeDirection(metric.metric) < 0;
  const signalColor = regression ? "#ff6b6b" : "#4ee1aa";
  return <article className="border p-4" style={{ borderColor: metric.alert ? signalColor : "#26332e", background: "#0e1412" }}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        {metric.group_id && <div className="text-xs mb-1" style={{ color: "#94a39d" }}>Group: <span title={metric.group_id}>{group?.label || metric.group_id}</span></div>}
        <div className="font-semibold">{metricLabel(metric.metric)}</div>
      </div>
      {metric.alert && <span className="text-xs font-mono px-2 py-1" style={{ color: signalColor, background: regression ? "rgba(255,107,107,.1)" : "rgba(78,225,170,.1)", borderRadius: 2 }}>{regression ? "REGRESSION" : "IMPROVEMENT"}</span>}
    </div>
    {numeric
      ? <div className="grid grid-cols-2 gap-3 mt-4 text-sm" aria-label="Reference and current score medians">
        <div>Reference median <strong>{metric.reference_value.toFixed(1)}</strong></div>
        <div>Current median <strong>{metric.current_value.toFixed(1)}</strong></div>
      </div>
      : <div className="space-y-2 mt-4" aria-label="Reference and current comparison chart">
        <ComparisonBar label="Reference" value={metric.reference_value} color="#57746a" />
        <ComparisonBar label="Current" value={metric.current_value} color={signalColor} />
      </div>}
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-px mt-4" style={{ background: "#26332e" }}>
      <div className="p-3" style={{ background: "#111715" }}><div className="text-xs" style={{ color: "#94a39d" }}>{numeric ? "Rank effect" : "Change"}</div><div className="font-semibold mt-1" style={{ color: signalColor }}>{numeric ? `${metric.effect >= 0 ? "+" : ""}${metric.effect.toFixed(3)} · ${metric.movement?.replaceAll("_", " ") || "unclassified"}` : `${metric.effect >= 0 ? "+" : ""}${(100 * metric.effect).toFixed(1)}pp`}</div></div>
      <div className="p-3" style={{ background: "#111715" }}><div className="text-xs" style={{ color: "#94a39d" }}>Raw p-value</div><div className="font-semibold mt-1">{pValue(metric.p_value)}</div></div>
      <div className="p-3" style={{ background: "#111715" }}><div className="text-xs" style={{ color: "#94a39d" }}>Adjusted p-value</div><div className="font-semibold mt-1">{pValue(metric.p_adjusted)}</div></div>
      <div className="p-3" style={{ background: "#111715" }}><div className="text-xs" style={{ color: "#94a39d" }}>Eligible samples</div><div className="font-semibold mt-1">{metric.reference_n} → {metric.current_n}</div></div>
    </div>
    {evidence && <div className="text-xs mt-3" style={{ color: "#94a39d" }}>Evidence coverage: {evidence.reference_evaluable} → {evidence.current_evaluable} evaluable · {evidence.reference_unclear} → {evidence.current_unclear} unclear · {evidence.reference_missing} → {evidence.current_missing} not judged · {evidence.reference_error} → {evidence.current_error} judge errors</div>}
    {metric.alert && <div className="mt-4 p-3 border" style={{ borderColor: "#26332e", background: "#111715" }}>
      <div className="text-xs font-medium" style={{ color: "#f2b84b" }}>Investigation next step</div>
      <div className="text-sm mt-1">Compare the current examples with the reference examples, then check model, prompt, and configuration changes before acting. Verdict has detected a change; it has not assigned a root cause.</div>
    </div>}
    {metric.alert && (referenceEvidenceUnitIds.length > 0 || currentEvidenceUnitIds.length > 0) && <div className="grid sm:grid-cols-2 gap-4 mt-4 pt-4 border-t" style={{ borderColor: "#26332e" }}>
      <EvidenceLinks label="Reference examples" unitIds={referenceEvidenceUnitIds} onOpenTrace={onOpenTrace} />
      <EvidenceLinks label="Current examples" unitIds={currentEvidenceUnitIds} onOpenTrace={onOpenTrace} />
    </div>}
  </article>;
}

const evidenceTraceOpener = (onOpenTrace, evaluators, policy) => {
  if (!onOpenTrace || policy?.analysis_unit === "conversation") return null;
  if (!policy?.evaluator_fingerprint) {
    return (traceId) => onOpenTrace(traceId, null);
  }
  const identity = evaluators.find(
    (candidate) => candidate.fingerprint === policy.evaluator_fingerprint,
  );
  return identity?.id
    ? (traceId) => onOpenTrace(traceId, identity.id)
    : null;
};

const frozenEvidenceIds = (summary, metric, value) => {
  const counts = (summary?.metrics || []).find(
    (item) => (item.group_id || null) === (metric.group_id || null)
      && item.metric === metric.metric,
  );
  return (value ? counts?.true_unit_ids : counts?.false_unit_ids) || [];
};

export function MonitorComparisonMetrics({
  comparison, referenceSummary = null, currentSummary = null, onOpenTrace = null,
}) {
  const keyFor = (item) => JSON.stringify([item.group_id || null, item.metric]);
  const coverage = new Map(
    (comparison?.metric_coverage || []).map((item) => [keyFor(item), item]),
  );
  const metrics = new Map(
    (comparison?.metrics || []).map((item) => [keyFor(item), item]),
  );
  const groups = new Map(
    (comparison?.groups || []).map((item) => [item.group_id, item]),
  );
  const cells = [...new Set([...metrics.keys(), ...coverage.keys()])]
    .sort((left, right) => Number(Boolean(metrics.get(right)?.alert)) - Number(Boolean(metrics.get(left)?.alert)));
  const alerted = cells.filter((key) => metrics.get(key)?.alert);
  const stable = cells.filter((key) => !metrics.get(key)?.alert);
  const renderCell = (key) => {
    const metric = metrics.get(key);
    const evidence = coverage.get(key);
    const row = metric || evidence;
    const group = groups.get(row.group_id);
    return metric
      ? <MetricComparisonCard key={key} metric={metric} evidence={evidence} group={group}
        referenceEvidenceUnitIds={metric.alert && metric.kind !== "number" ? frozenEvidenceIds(referenceSummary, metric, metric.effect < 0) : []}
        currentEvidenceUnitIds={metric.alert && metric.kind !== "number" ? frozenEvidenceIds(currentSummary, metric, metric.effect >= 0) : []}
        onOpenTrace={onOpenTrace} />
      : <div key={key} className="border p-3 text-sm" style={{ borderColor: "#26332e" }}>
        {row.group_id && <div className="text-xs mb-2" style={{ color: "#94a39d" }}>Group: <span title={row.group_id}>{group?.label || row.group_id}</span></div>}
        <span className="font-mono">{metricLabel(row.metric)}</span>
        <span className="ml-3" style={{ color: "#f2b84b" }}>{row.metric.startsWith("score.") ? "No supported numeric comparison yet" : "No PASS/FAIL comparison yet"}</span>
        <div className="text-xs mt-2" style={{ color: "#94a39d" }}>Evidence coverage: {evidence.reference_evaluable} → {evidence.current_evaluable} evaluable · {evidence.reference_unclear} → {evidence.current_unclear} unclear · {evidence.reference_missing} → {evidence.current_missing} not judged · {evidence.reference_error} → {evidence.current_error} judge errors</div>
      </div>;
  };
  return <div className="mt-4 space-y-3">
    {alerted.map(renderCell)}
    {stable.length > 0 && <details open={alerted.length === 0} className="border" style={{ borderColor: "#26332e" }}>
      <summary className="p-3 text-sm cursor-pointer" style={{ color: "#94a39d" }}>{stable.length} stable or incomplete comparison{stable.length === 1 ? "" : "s"}</summary>
      <div className="space-y-3 p-3 pt-0">{stable.map(renderCell)}</div>
    </details>}
  </div>;
}

function MonitorSnapshot({ response, evaluators, fallbackTarget, onOpenTrace }) {
  const snapshot = response.snapshot;
  const manifest = snapshot.manifest;
  const comparison = snapshot.comparison;
  const policy = response.policy;
  const candidate = response.policyState === "candidate" || response.state === "candidate";
  const measurement = evaluators.find(
    (identity) => identity.fingerprint === policy?.evaluator_fingerprint,
  );
  const openEvidenceTrace = evidenceTraceOpener(onOpenTrace, evaluators, policy);
  const collecting = manifest.prospective_open === true;
  const pendingEvaluations = manifest.pending_evaluator_units?.length || 0;
  const target = policy?.prospective_target || fallbackTarget;
  const awaitingEvaluator = collecting
    && manifest.current_unit_ids.length >= target && pendingEvaluations > 0;
  const label = response.state === "requires_rebootstrap" ? "Re-bootstrap required"
    : awaitingEvaluator ? `Awaiting ${pendingEvaluations} evaluator results`
      : collecting ? `Collecting ${manifest.current_unit_ids.length}/${target}`
      : comparison.status === "insufficient" ? "Insufficient evidence"
        : comparison.status.replaceAll("_", " ");
  const alertMetrics = (comparison.metrics || []).filter((metric) => metric.alert);
  const affectedGroups = new Set(alertMetrics.map((metric) => metric.group_id || "all-traffic"));
  return <section className="border p-5" style={box}>
    <div className="flex flex-wrap gap-3 items-center justify-between">
      <div><div className="text-xs font-mono" style={{ color: candidate ? "#f2b84b" : "#4ee1aa" }}>DRIFT ANALYSIS · {candidate ? "EXPLORATORY HISTORICAL COMPARISON" : "ACTIVE PROSPECTIVE MONITOR"}</div><div className="font-semibold mt-1">{alertMetrics.length > 0 ? `${alertMetrics.length} drift signal${alertMetrics.length === 1 ? "" : "s"} across ${affectedGroups.size} affected segment${affectedGroups.size === 1 ? "" : "s"}` : label}</div></div>
      <div className="text-sm" style={{ color: "#94a39d" }}>{manifest.reference_unit_ids.length} reference → {manifest.current_unit_ids.length} current</div>
    </div>
    {!!response.coverage && <div className="text-xs mt-3" style={{color:"#94a39d"}}>Captured {response.coverage.captured} → eligible {response.coverage.eligible} → judged {response.coverage.judged} · excluded: missing event time {response.coverage.missingEventTime}, unknown ending {response.coverage.unknownClosure}, incomplete evidence {response.coverage.incompleteEvidence}{policy.response_aggregation && <span> · replies {response.coverage.assessedReplies}/{response.coverage.expectedReplies} assessed · partial conversations {response.coverage.partiallyAssessed} · known failures in partial conversations {response.coverage.knownFailuresInPartial} · interrupted replies excluded {response.coverage.excludedInterruptedReplies}</span>}</div>}
    {response.earlyIndicators?.length > 0 && <details className="mt-3 text-sm"><summary>Measured changes and sample sizes · exploratory</summary><p className="text-xs mt-2" style={{color:"#94a39d"}}>These observations remain useful before enough evidence exists for an alert. Numeric values are medians; supported numeric alerts test ranks, not medians. Historical comparisons are exploratory.</p>{response.earlyIndicators.map(r=><div key={`${r.group_id || "all"}:${r.metric}`} className="mt-2">{metricLabel(r.metric)}{r.group_id && ` · group ${r.group_id.slice(0,8)}`} · {r.reference_value == null ? "—" : r.kind === "number" ? r.reference_value.toFixed(1) : `${(100*r.reference_value).toFixed(1)}%`} → {r.current_value == null ? "—" : r.kind === "number" ? r.current_value.toFixed(1) : `${(100*r.current_value).toFixed(1)}%`} · n {r.reference_n} → {r.current_n}</div>)}</details>}
    <div className="mt-4 h-8 flex overflow-hidden border" style={{ borderColor: "#26332e" }}><div style={{ width: `${100 * manifest.reference_unit_ids.length / Math.max(1, manifest.reference_unit_ids.length + manifest.current_unit_ids.length)}%`, background: "#1f5f4b" }} /><div className="flex-1" style={{ background: "#295a78" }} /></div>
    <div className="mt-3 text-xs" style={{ color: "#94a39d" }}>
      {awaitingEvaluator
        ? `Membership is fixed at ${manifest.current_unit_ids.length}/${target}; no comparison or alert decision will run until its evaluator evidence is complete.`
        : collecting ? `Prospective bucket ${manifest.current_unit_ids.length}/${target}; no comparison or alert decision has run.` : `Completed comparison look ${manifest.comparison_index} · alert threshold ${comparison.alpha_threshold.toPrecision(3)} · ${policy?.sequential_method || "configured sequential correction"}`}
    </div>
    <div className="mt-2 text-xs" style={{ color: "#94a39d" }}>Measurement: {policy?.evaluator_fingerprint ? (measurement?.label || `stored evaluator ${policy.evaluator_fingerprint.slice(0, 8)}`) : policy?.analysis_unit === "conversation" ? "Recorded conversation completion only" : "deterministic trace checks only"}</div>
    <div className="mt-1 text-xs" style={{ color: "#94a39d" }}>Facet: {policy?.grouping_mode === "cluster" ? `frozen clusters · ${policy.cluster_registry_version_id || "registry unavailable"}` : policy?.grouping_mode === "provider_model" ? "provider and model" : policy?.grouping_mode === "population" ? "language and workflow" : policy?.analysis_unit === "conversation" ? "all eligible conversations" : "all eligible calls"}</div>
    <MonitorComparisonMetrics comparison={comparison}
      referenceSummary={manifest.reference_summary}
      currentSummary={manifest.current_summary}
      onOpenTrace={openEvidenceTrace} />
    {comparison.status === "insufficient" && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{awaitingEvaluator ? "Run the selected evaluator, then run this monitor again. To stop measuring that evaluator, preview and activate a replacement monitor." : collecting ? "No statistical test was run because the prospective bucket is still collecting." : "The bucket closed, but no metric met its configured eligible-unit minimums; no alert/no-alert conclusion was produced."}</p>}
    {comparison.unseen_group_share > 0 && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{comparison.status === "reference_stale" ? "Comparison suspended" : "Coverage note"}: {(100 * comparison.unseen_group_share).toFixed(1)}% of current {policy?.analysis_unit === "conversation" ? "conversations" : "traces"} are outside the frozen {policy?.grouping_mode === "cluster" ? "cluster" : policy?.grouping_mode === "population" ? "language/workflow" : "provider/model"} reference{comparison.unassigned_group_share > 0 ? ` (${(100 * comparison.unassigned_group_share).toFixed(1)}% are unassigned)` : ""}.{comparison.status === "reference_stale" ? " Review the policy before creating a new candidate; Verdict did not silently rebase it." : ` These ${policy?.analysis_unit === "conversation" ? "conversations" : "traces"} were excluded from like-for-like metric tests.`}</p>}
  </section>;
}

export function Monitor({ configUrl, evaluation = {}, initialState = null, view = "history", onChanged = null, onOpenTrace = null }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const evaluators = (evaluation.availableIdentities || []).filter(
    (identity, index, rows) => identity.complete && identity.fingerprint
      && rows.findIndex((other) => other.fingerprint === identity.fingerprint) === index,
  );
  const requestEpoch=useRef(0);
  const [token, setToken] = useState(null);
  const [active, setActive] = useState(initialState?.active || null);
  const [candidate, setCandidate] = useState(initialState?.candidate || null);
  const [descriptive, setDescriptive] = useState(null);
  const [agentEvaluators, setAgentEvaluators] = useState(initialState?.agentEvaluators || []);
  const [agentEvaluatorDiscoveryTruncated, setAgentEvaluatorDiscoveryTruncated] = useState(initialState?.agentEvaluatorDiscoveryTruncated || false);
  const [error, setError] = useState(null);
  const [sessionEvaluators, setSessionEvaluators] = useState([]);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    windowMode: "count", referenceRatio: 0.8, minimumReference: 30,
    minimumCurrent: 30, prospectiveTarget: 30, minimumEffect: 0.1,
    analysisUnit: "trace", groupingMode: "none",
    evaluatorFingerprint: "",
    referenceStart: "", referenceEnd: "", currentStart: "", currentEnd: "",
  });
  useEffect(() => {
    let current=true;
    setToken(null); setActive(null); setCandidate(null); setDescriptive(null);
    setAgentEvaluators([]); setSessionEvaluators([]); setAgentEvaluatorDiscoveryTruncated(false);
    setError(null); setBusy(false);
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((response) => response.json()),
      fetch(`${root}/api/monitor?unit=${form.analysisUnit === "conversation" ? "conversation" : "trace"}`, { credentials: "same-origin" }).then((response) => response.json()),
      form.analysisUnit === "conversation"
        ? fetch(`${root}/api/data/sessions`, {credentials:"same-origin"}).then(r=>r.json())
        : Promise.resolve({evaluatorIdentities:[]}),
    ]).then(([config, monitor, sessions]) => {
      if(!current) return;
      const state = monitorStateParts(monitor);
      setSessionEvaluators((sessions.evaluatorIdentities || []).map(e=>({...e,label:`${e.rubric} v${e.version} · ${e.target} · ${e.model}`})));
      setToken(config.setupToken);
      setActive(form.analysisUnit === "logical_session" ? null : state.active);
      setCandidate(form.analysisUnit === "logical_session" ? null : state.candidate);
      setAgentEvaluators(monitor.agentEvaluators || []);
      setAgentEvaluatorDiscoveryTruncated(Boolean(monitor.agentEvaluatorDiscoveryTruncated));
    })
      .catch((failure) => {if(current)setError(String(failure));});
    return()=>{current=false; requestEpoch.current++;};
  }, [configUrl, root, form.analysisUnit]);

  async function post(path, payload) {
    const epoch=++requestEpoch.current;
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
      const body = await response.json();
      if(epoch !== requestEpoch.current) return null;
      const result = !response.ok && body.exploration ? body.exploration : body;
      if (!response.ok && !body.exploration) throw new Error(body.error || `HTTP ${response.status}`);
      // One owner commits action results before yielding back to the buttons.
      if (path === "/api/monitor/preview") {
        setDescriptive(result.state === "descriptive" ? result : null);
        setCandidate(result.state === "descriptive" ? null : result);
      } else {
        setActive(result);
        if (path === "/api/monitor/activate") setCandidate(null);
      }
      onChanged?.();
    } catch (failure) { if(epoch === requestEpoch.current)setError(String(failure)); }
    finally { if(epoch === requestEpoch.current)setBusy(false); }
  }

  const update = (name, value) => {
    requestEpoch.current++;
    setError(null); setBusy(false);
    setCandidate(null);
    setDescriptive(null);
    if (name === "analysisUnit") setActive(null);
    setForm((current) => ({
      ...current,
      [name]: value,
      ...(name === "evaluatorFingerprint" && current.analysisUnit === "conversation" ? {responseAggregation: sessionEvaluators.find(e=>e.fingerprint===value)?.target === "response" ? "all_completed_replies_v1" : null} : {}),
      ...(name === "analysisUnit" ? { evaluatorFingerprint: "", groupingMode: "none", responseAggregation:null } : {}),
    }));
  };
  const logicalSession = form.analysisUnit === "logical_session";
  const measurements = form.analysisUnit === "conversation" ? sessionEvaluators : logicalSession ? agentEvaluators : evaluators;
  const requiresRebootstrap = active?.state === "requires_rebootstrap";
  return <div className="max-w-5xl space-y-4">
    <section className="border p-5 flex flex-wrap items-end gap-4" style={box}>
      <label className="text-sm flex-1 min-w-48">Analysis unit<select value={form.analysisUnit} onChange={(event) => update("analysisUnit", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="conversation">Conversation (one recorded session)</option><option value="trace">Genuine model call</option><option value="logical_session">Logical session (descriptive)</option></select></label>
      {!logicalSession && canRunActiveMonitor(active) && <button disabled={!token || busy} onClick={() => post(`/api/monitor/run?unit=${form.analysisUnit === "conversation" ? "conversation" : "trace"}`)} className="border px-4 py-2 text-sm">Run next cohort now</button>}
    </section>
    {view === "status" && !active && !candidate && <section className="border p-5" style={box}><div className="text-xs font-mono" style={{ color: "#f2b84b" }}>MONITORING</div><h2 className="text-lg font-semibold mt-1">{logicalSession ? "Logical sessions are descriptive only" : "No comparison configured for this analysis unit"}</h2><p className="text-sm mt-2" style={{ color: "#94a39d" }}>{logicalSession ? "Open Compare History for an observational comparison. Logical sessions cannot activate an alert monitor." : "Open Compare History to create a historical comparison for the selected unit. Activate it only if new traffic will continue arriving."}</p></section>}
    {error && <div role="alert" className="border p-4" style={{ ...box, color: "#ff6b6b" }}>{error}</div>}
    {candidate && active && <div role="status" className="border p-4 text-sm" style={{ ...box, color: "#f2b84b" }}>A newer historical candidate is shown first. The existing prospective monitor remains active until you explicitly activate the candidate.</div>}
    {requiresRebootstrap && <div role="alert" className="border p-4" style={{ ...box, color: "#f2b84b" }}>{active.rebootstrapReason} Configure the replacement in Compare History and select Preview comparison.</div>}
    {descriptive && (descriptive.activationAllowed === false ? <HistoricalObservations preview={descriptive} /> : <LogicalSessionPreview preview={descriptive} />)}
    {candidate?.snapshot && <MonitorSnapshot response={candidate} evaluators={measurements} fallbackTarget={form.prospectiveTarget} onOpenTrace={onOpenTrace} />}
    {active?.snapshot && <MonitorSnapshot response={active} evaluators={measurements} fallbackTarget={form.prospectiveTarget} onOpenTrace={onOpenTrace} />}
    {view === "history" && <details open={!candidate && !active && !descriptive} className="border" style={box}>
      <summary className="p-5 cursor-pointer"><span className="text-xs font-mono" style={{ color: "#4ee1aa" }}>COMPARISON SETTINGS</span><span className="block text-sm mt-1" style={{ color: "#94a39d" }}>Choose cohorts, measurement, and activation policy</span></summary>
      <div className="px-5 pb-5 border-t" style={{ borderColor: "#26332e" }}>
      <h2 className="text-lg font-semibold mt-1">Explore first, then activate one immutable monitor</h2>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>Membership is chosen from event time before metric outcomes are compared. No clustering is required. Preview is exploratory; only an activated policy can become authoritative.</p>
      <div className="grid sm:grid-cols-2 gap-4 mt-5">
        <label className="text-sm">Window mode<select value={form.windowMode} onChange={(event) => update("windowMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="count">Count cohorts</option><option value="explicit">Explicit date ranges</option></select></label>
        {form.windowMode === "count" && <label className="text-sm">Reference share<input type="number" min="0.5" max="0.95" step="0.05" value={form.referenceRatio} onChange={(event) => update("referenceRatio", Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>}
        <label className="text-sm">Measurement<select value={form.evaluatorFingerprint} onChange={(event) => update("evaluatorFingerprint", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="">{logicalSession ? "Deterministic agent checks only" : form.analysisUnit === "conversation" ? "Recorded conversation completion only" : "Deterministic trace checks only"}</option>{measurements.map((identity) => <option key={identity.fingerprint} value={identity.fingerprint}>{identity.label}</option>)}</select><span className="block text-xs mt-1" style={{ color: "#94a39d" }}>{form.evaluatorFingerprint ? "Compares existing stored judgments; this preview makes no judge calls." : logicalSession ? "Compares completed execution and final-output presence by logical session." : form.analysisUnit === "conversation" ? "Compares recorded ending status; select an evaluator to measure quality." : "Compares provider errors, empty responses, and refusal-like language."}</span>{logicalSession && <AgentEvaluatorDiscoveryNote truncated={agentEvaluatorDiscoveryTruncated} />}</label>
        <label className="text-sm">Comparison facet<select disabled={logicalSession} value={form.groupingMode} onChange={(event) => update("groupingMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="none">{logicalSession ? "No grouping for logical sessions" : form.analysisUnit === "conversation" ? "All eligible conversations" : "All eligible calls (recommended)"}</option>{!logicalSession && <><option value="provider_model">Provider and model</option>{form.analysisUnit === "conversation" ? <option value="population">Language and workflow</option> : <option value="cluster">Active reviewed cluster</option>}</>}</select></label>
        {form.windowMode === "explicit" && ["referenceStart", "referenceEnd", "currentStart", "currentEnd"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="datetime-local" value={form[name]} onChange={(event) => update(name, event.target.value)} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
        {!logicalSession && ["minimumReference", "minimumCurrent", "prospectiveTarget"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="number" min="1" value={form[name]} onChange={(event) => update(name, Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
      </div>
      {form.responseAggregation && <p className="text-sm mt-4" style={{color:"#f2b84b"}}>Any completed reply fails, among fully evaluated conversations. Missing, error or unclear replies exclude that conversation from the pass/fail denominator. Known failures remain visible in Evaluate. Numeric response rubrics are not supported by this monitor.</p>}
      <div className="flex flex-wrap gap-2 mt-5">
        <button disabled={!token || busy} onClick={() => post("/api/monitor/preview", monitorRequest(form))} className="border px-4 py-2 text-sm">Preview comparison</button>
        {!logicalSession && candidate && <button disabled={!token || busy} onClick={() => post("/api/monitor/activate", { policyId: candidate.policy.policy_id, expectedActivePolicyId: active?.policy?.policy_id || null })} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Activate monitor</button>}
      </div>
      </div>
    </details>}
    {active?.approvedHistoricalSnapshot && <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#f2b84b" }}>APPROVED HISTORICAL PREVIEW</div>
      <div className="font-semibold mt-1">{active.approvedHistoricalSnapshot.comparison.status.replaceAll("_", " ")}</div>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>This is the historical comparison used to approve the policy. Activation froze its reference cohort and opened a new prospective bucket; it did not reuse the historical current cohort as new traffic.</p>
      <div className="text-sm mt-3">{active.approvedHistoricalSnapshot.manifest.reference_unit_ids.length} historical reference → {active.approvedHistoricalSnapshot.manifest.current_unit_ids.length} historical current</div>
      <MonitorComparisonMetrics comparison={active.approvedHistoricalSnapshot.comparison}
        referenceSummary={active.approvedHistoricalSnapshot.manifest.reference_summary}
        currentSummary={active.approvedHistoricalSnapshot.manifest.current_summary}
        onOpenTrace={evidenceTraceOpener(onOpenTrace, measurements, active.policy)} />
    </section>}
  </div>;
}
