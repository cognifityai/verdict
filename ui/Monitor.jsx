import React, { useEffect, useState } from "react";
import { conversationMonitorRequest, monitorRequest } from "./monitor-form.mjs";
import { EvidenceExplain } from "./EvidenceExplain.jsx";

const box = { borderColor: "#26332e", background: "#111715" };

const utcWindowTime = (value) => new Date(value).toLocaleString(undefined, {
  timeZone: "UTC", year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", timeZoneName: "short",
});

function detailGradeState(detail, dimension) {
  const grade = detail.assessments.find((item) => item.target_position == null);
  if (!grade) return "ungraded";
  if (grade.status === "error") return "error";
  return grade.status === "completed" ? grade.dimensions?.[dimension]?.state ?? "invalid" : "invalid";
}

export function monitorStateParts(monitor) {
  const legacy = monitor?.policy && monitor?.snapshot ? monitor : null;
  return {
    active: monitor?.active || (["active", "requires_rebootstrap"].includes(monitor?.state) ? legacy : null),
    candidate: monitor?.candidate || (monitor?.state === "candidate" ? legacy : null),
  };
}

export const metricLabel = (metric) => {
  if (metric.startsWith("judge.") && metric.endsWith(".pass")) {
    return `${metric.slice(6, -5).replaceAll("_", " ")} pass rate`;
  }
  return ({
    provider_error: "Provider error rate",
    response_empty: "Blank captured response-text rate",
    refusal_signature: "Refusal-language rate",
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

function ConversationRate({ label, summary, onOpen }) {
  return <article className="border p-3 text-sm" style={box}>
    <div className="font-semibold">{label}</div>
    <div className="mt-1">{summary.passRate == null ? "No PASS/FAIL grades" : `${pct(summary.passRate)} PASS · 95% interval ${pct(summary.interval[0])}–${pct(summary.interval[1])}`}</div>
    <div className="text-xs mt-1" style={{ color: "#94a39d" }}>Captured {summary.captured} · eligible {summary.eligible} · PASS {summary.pass} · FAIL {summary.fail} · UNCLEAR {summary.unclear} · judge errors {summary.error} · ungraded eligible {summary.ungraded} · not evaluable {summary.ineligible}. Rate denominator: {summary.evaluable} PASS/FAIL grades.</div>
    <div className="flex flex-wrap gap-2 mt-2">{["fail", "pass", "unclear", "error", "ungraded", "ineligible"].flatMap((state) =>
      summary.examples[state].map((example) => <button key={`${state}-${example.id}`} type="button"
        className="underline text-xs font-mono" onClick={() => onOpen(example)}
        title={example.id}>{state}: {example.id.slice(-8)}</button>))}</div>
  </article>;
}

export function ConversationPreview({ preview, onOpen }) {
  return <section className="border p-5 space-y-3" style={box}>
    <div className="text-xs font-mono" style={{ color: "#f2b84b" }}>DESCRIPTIVE STORED GRADES</div>
    <h2 className="font-semibold">{preview.dimension} · Base versus Current</h2>
    <p className="text-sm" style={{ color: "#94a39d" }}>Current stored grades in the selected UTC windows. This result is not saved, makes no judge calls, and does not create an alert. Grade accuracy requires human-label calibration. Transcript corrections can change the next result; source labels are not semantic clusters.</p>
    {preview.gradeEvidenceCount === 0 && <p role="alert" className="text-sm" style={{ color: "#f2b84b" }}>No grades from this evaluator in the selected windows. Check the fingerprint and dates, or grade eligible conversations in Evaluator Lab.</p>}
    <div className="grid sm:grid-cols-2 gap-3">
      <ConversationRate label={`Base · ${utcWindowTime(preview.referenceStart)} to ${utcWindowTime(preview.referenceEnd)}`} summary={preview.reference} onOpen={onOpen} />
      <ConversationRate label={`Current · ${utcWindowTime(preview.currentStart)} to ${utcWindowTime(preview.currentEnd)}`} summary={preview.current} onOpen={onOpen} />
    </div>
    <p className="text-sm">{preview.effect == null ? "No complete rate comparison" : `Stored-grade PASS-rate change: ${(100 * preview.effect).toFixed(1)} percentage points`} · {preview.reference.evaluable} versus {preview.current.evaluable} graded conversations.</p>
    {preview.groups.length > 0 && <div className="space-y-3"><h3 className="font-semibold">By source label: {preview.labelKey}</h3>{preview.groups.map((group) => <div key={group.value == null ? "missing-label" : `value:${group.value}`} className="border p-3" style={box}>
      <div className="font-semibold">{group.label} · share of eligible conversations {group.referenceShare == null ? "—" : pct(group.referenceShare)} → {group.currentShare == null ? "—" : pct(group.currentShare)}</div>
      <div className="text-sm mt-1">{group.effect == null ? "No complete within-group rate comparison" : `Within-group PASS-rate change: ${(100 * group.effect).toFixed(1)} percentage points`}</div>
      <div className="grid sm:grid-cols-2 gap-3 mt-2"><ConversationRate label="Base" summary={group.reference} onOpen={onOpen} /><ConversationRate label="Current" summary={group.current} onOpen={onOpen} /></div>
    </div>)}</div>}
  </section>;
}

export const canRunActiveMonitor = (active) => active?.state === "active";

export function referenceReadyForActivation(candidate) {
  const manifest = candidate?.snapshot?.manifest;
  const floor = candidate?.policy?.minimum_reference;
  return Number.isInteger(floor) && floor > 0
    && manifest?.reference_unit_ids?.length >= floor
    && manifest?.reference_summary?.metrics?.some((metric) =>
      Number(metric.true_count || 0) + Number(metric.false_count || 0) >= floor) === true;
}

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
  || ["agent.execution_completed", "agent.final_output_present"].includes(metric);

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
  const direction = metric.effect * changeDirection(metric.metric);
  const regression = direction < 0;
  const textOnly = metric.metric === "response_empty";
  const signalColor = textOnly ? "#f2b84b" : regression ? "#ff6b6b" : "#4ee1aa";
  return <article className="border p-4" style={{ borderColor: metric.alert ? signalColor : "#26332e", background: "#0e1412" }}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        {metric.group_id && <div className="text-xs mb-1" style={{ color: "#94a39d" }}>Group: <span title={metric.group_id}>{group?.label || metric.group_id}</span></div>}
        <div className="font-semibold">{metricLabel(metric.metric)}</div>
        {metric.metric === "response_empty" && <div className="text-xs mt-1" style={{ color: "#94a39d" }}>This measures captured text, not whether the agent succeeded. A tool-only completion can have no response text.</div>}
      </div>
      {metric.alert && <span className="text-xs font-mono px-2 py-1" style={{ color: signalColor, background: textOnly ? "rgba(242,184,75,.1)" : regression ? "rgba(255,107,107,.1)" : "rgba(78,225,170,.1)", borderRadius: 2 }}>{textOnly ? "TEXT PATTERN CHANGED" : regression ? "REGRESSION" : "IMPROVEMENT"}</span>}
    </div>
    <div className="space-y-2 mt-4" aria-label="Reference and current comparison chart">
      <ComparisonBar label="Reference" value={metric.reference_value} color="#57746a" />
      <ComparisonBar label="Current" value={metric.current_value} color={signalColor} />
    </div>
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-px mt-4" style={{ background: "#26332e" }}>
      <div className="p-3" style={{ background: "#111715" }}><div className="text-xs" style={{ color: "#94a39d" }}>Change</div><div className="font-semibold mt-1" style={{ color: signalColor }}>{metric.effect >= 0 ? "+" : ""}{(100 * metric.effect).toFixed(1)}pp</div></div>
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
  if (!onOpenTrace) return null;
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
        referenceEvidenceUnitIds={metric.alert ? frozenEvidenceIds(referenceSummary, metric, metric.effect < 0) : []}
        currentEvidenceUnitIds={metric.alert ? frozenEvidenceIds(currentSummary, metric, metric.effect >= 0) : []}
        onOpenTrace={onOpenTrace} />
      : <div key={key} className="border p-3 text-sm" style={{ borderColor: "#26332e" }}>
        {row.group_id && <div className="text-xs mb-2" style={{ color: "#94a39d" }}>Group: <span title={row.group_id}>{group?.label || row.group_id}</span></div>}
        <span className="font-mono">{metricLabel(row.metric)}</span>
        <span className="ml-3" style={{ color: "#f2b84b" }}>No PASS/FAIL comparison yet</span>
        <div className="text-xs mt-2" style={{ color: "#94a39d" }}>Evidence coverage: {evidence.reference_evaluable} → {evidence.current_evaluable} evaluable · {evidence.reference_unclear} → {evidence.current_unclear} unclear · {evidence.reference_missing} → {evidence.current_missing} not judged · {evidence.reference_error} → {evidence.current_error} judge errors</div>
      </div>;
  };
  return <div className="mt-4 space-y-3">
    {alerted.map(renderCell)}
    {stable.length > 0 && <details className="border" style={{ borderColor: "#26332e" }}>
      <summary className="p-3 text-sm cursor-pointer" style={{ color: "#94a39d" }}>{stable.length} metric detail{stable.length === 1 ? "" : "s"} · open to inspect unchanged results or missing evidence</summary>
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
    <div className="mt-4 h-8 flex overflow-hidden border" style={{ borderColor: "#26332e" }}><div style={{ width: `${100 * manifest.reference_unit_ids.length / Math.max(1, manifest.reference_unit_ids.length + manifest.current_unit_ids.length)}%`, background: "#1f5f4b" }} /><div className="flex-1" style={{ background: "#295a78" }} /></div>
    <div className="mt-3 text-xs" style={{ color: "#94a39d" }}>
      {awaitingEvaluator
        ? `Membership is fixed at ${manifest.current_unit_ids.length}/${target}; no comparison or alert decision will run until its evaluator evidence is complete.`
        : collecting ? `Prospective bucket ${manifest.current_unit_ids.length}/${target}; no comparison or alert decision has run.` : `Completed comparison look ${manifest.comparison_index} · alert threshold ${comparison.alpha_threshold.toPrecision(3)} · ${policy?.sequential_method || "configured sequential correction"}`}
    </div>
    <div className="mt-2 text-xs" style={{ color: "#94a39d" }}>Measurement: {policy?.evaluator_fingerprint ? (measurement?.label || `stored evaluator ${policy.evaluator_fingerprint.slice(0, 8)}`) : "deterministic trace checks only"}</div>
    <div className="mt-1 text-xs" style={{ color: "#94a39d" }}>Facet: {policy?.grouping_mode === "cluster" ? `frozen clusters · ${policy.cluster_registry_version_id || "registry unavailable"}` : policy?.grouping_mode === "provider_model" ? "provider and model" : "all eligible calls"}</div>
    {!collecting && comparison.status === "no_alert" && <p className="text-sm mt-4" style={{ color: "#94a39d" }}>No selected metric crossed the configured alert threshold in this {candidate ? "historical preview" : "completed prospective comparison"}. Open metric details below to inspect the underlying rates and sample sizes.</p>}
    <MonitorComparisonMetrics comparison={comparison}
      referenceSummary={manifest.reference_summary}
      currentSummary={manifest.current_summary}
      onOpenTrace={openEvidenceTrace} />
    {comparison.status === "insufficient" && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{awaitingEvaluator ? "Run the selected evaluator, then run this monitor again. To stop measuring that evaluator, preview and activate a replacement monitor." : collecting ? "No statistical test was run because the prospective bucket is still collecting." : "The bucket closed, but no metric met its configured eligible-unit minimums; no alert/no-alert conclusion was produced."}</p>}
    {comparison.unseen_group_share > 0 && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{comparison.status === "reference_stale" ? "Comparison suspended" : "Coverage note"}: {(100 * comparison.unseen_group_share).toFixed(1)}% of current traces are outside the frozen {policy?.grouping_mode === "cluster" ? "cluster" : "provider/model"} reference{comparison.unassigned_group_share > 0 ? ` (${(100 * comparison.unassigned_group_share).toFixed(1)}% are unassigned)` : ""}.{comparison.status === "reference_stale" ? " Review the policy before creating a new candidate; Verdict did not silently rebase it." : " These traces were excluded from like-for-like metric tests."}</p>}
  </section>;
}

export function Monitor({ configUrl, evaluation = {}, initialState = null, view = "history", traceCount = 0, totalTraces = null, agentRunCount = 0, onChanged = null, onOpenTrace = null, onSetup = null, onPlan = null, onGroups = null }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const evaluators = (evaluation.availableIdentities || []).filter(
    (identity, index, rows) => identity.complete && identity.fingerprint
      && rows.findIndex((other) => other.fingerprint === identity.fingerprint) === index,
  );
  const [token, setToken] = useState(null);
  const [active, setActive] = useState(initialState?.active || null);
  const [candidate, setCandidate] = useState(initialState?.candidate || null);
  const [descriptive, setDescriptive] = useState(null);
  const [conversationDetail, setConversationDetail] = useState(null);
  const [agentEvaluators, setAgentEvaluators] = useState(initialState?.agentEvaluators || []);
  const [agentEvaluatorDiscoveryTruncated, setAgentEvaluatorDiscoveryTruncated] = useState(initialState?.agentEvaluatorDiscoveryTruncated || false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [draftApplied, setDraftApplied] = useState(false);
  const [form, setForm] = useState({
    windowMode: "count", referenceRatio: 0.8, minimumReference: 30,
    minimumCurrent: 30, prospectiveTarget: 30, minimumEffect: 0.1,
    analysisUnit: "trace", groupingMode: "none",
    evaluatorFingerprint: "",
    dimension: "", labelKey: "",
    referenceStart: "", referenceEnd: "", currentStart: "", currentEnd: "",
  });
  useEffect(() => {
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((response) => response.json()),
      fetch(`${root}/api/monitor`, { credentials: "same-origin" }).then((response) => response.json()),
    ]).then(([config, monitor]) => {
      const state = monitorStateParts(monitor);
      setToken(config.setupToken);
      setActive(state.active);
      setCandidate(state.candidate);
      setAgentEvaluators(monitor.agentEvaluators || []);
      setAgentEvaluatorDiscoveryTruncated(Boolean(monitor.agentEvaluatorDiscoveryTruncated));
    })
      .catch((failure) => setError(String(failure)));
  }, [configUrl, root]);

  async function post(path, payload) {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      return body;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(false); }
  }
  async function openConversation(example) {
    if (busy || !descriptive || descriptive.unit !== "conversation") return;
    setBusy(true); setError(null); setConversationDetail(null);
    try {
      const response = await fetch(`${root}/api/data/conversations/${encodeURIComponent(example.id)}?evaluator=${encodeURIComponent(descriptive.evaluatorFingerprint)}`, { credentials: "same-origin" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      setConversationDetail({ ...body, expectedRevision: example.revision,
        expectedState: example.qualityState, expectedReason: example.ineligibleReason });
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }

  const update = (name, value) => {
    setCandidate(null);
    setDescriptive(null);
    setConversationDetail(null);
    setForm((current) => ({
      ...current,
      [name]: value,
      ...(name === "analysisUnit" ? { evaluatorFingerprint: "", groupingMode: "none",
        windowMode: value === "conversation" ? "explicit" : current.windowMode } : {}),
    }));
  };
  const logicalSession = form.analysisUnit === "logical_session";
  const conversation = form.analysisUnit === "conversation";
  const measurements = logicalSession ? agentEvaluators : evaluators;
  const requiresRebootstrap = active?.state === "requires_rebootstrap";
  const referenceReady = referenceReadyForActivation(candidate);
  const excludedTraceCount = Number.isInteger(totalTraces) && Number.isInteger(traceCount)
    && traceCount >= 0 && totalTraces >= traceCount
    ? totalTraces - traceCount : null;
  async function previewComparison() {
    const result = await post("/api/monitor/preview", conversation ? conversationMonitorRequest(form) : monitorRequest(form));
    if (result) {
      setConversationDetail(null);
      if (result.state === "descriptive") { setDescriptive(result); setCandidate(null); }
      else { setCandidate(result); setDescriptive(null); }
      onChanged?.();
    }
  }
  return <div className="max-w-5xl space-y-4">
    {view === "history" && <><div className="verdict-setup-steps" aria-label="Model drift setup steps"><strong>1 · Data eligibility</strong><span>2 · Review baseline</span><span>3 · Activate</span></div><section className="border p-5" style={{ borderColor: traceCount ? "#365b47" : "#6b5529", background: traceCount ? "#17251e" : "#211e18" }}><div className="text-xs font-mono" style={{ color: traceCount ? "#4ee1aa" : "#f2b84b" }}>{traceCount ? "GENUINE CALLS AVAILABLE" : "MODEL-CALL CAPTURE NEEDED"}</div><strong className="block mt-1">{traceCount ? `${traceCount.toLocaleString()} eligible completed model ${traceCount === 1 ? "call" : "calls"}` : "No eligible provider model calls"}</strong><p className="text-sm mt-2" style={{ color: "#94a39d" }}>{traceCount ? "Preview the historical reference and current cohorts before enabling a prospective alert. Counts alone do not establish sample readiness." : "Recorded agent runs can still support local issue checks; model-call drift requires instrumented provider calls."}</p></section></>}
    {view === "status" && !active && !candidate && <section className="border p-5" style={box}><div className="text-xs font-mono" style={{ color: "#94a39d" }}>OPTIONAL MODEL-CALL COMPARISON</div><h2 className="text-lg font-semibold mt-1">{traceCount ? "Compare captured provider calls when useful" : "No eligible provider calls for a statistical comparison"}</h2><p className="text-sm mt-2" style={{ color: "#94a39d" }}>{traceCount ? "Recorded issue checks above work independently. For provider-call drift, review a historical reference and current cohort before activating a separate prospective policy." : "The issue inbox above can still watch explicit failures in captured agent runs. Provider-call drift needs genuine completed model calls from an instrumented source."}</p></section>}
    {error && <div role="alert" className="border p-4" style={{ ...box, color: "#ff6b6b" }}>{error}</div>}
    {candidate && active && <div role="status" className="border p-4 text-sm" style={{ ...box, color: "#f2b84b" }}>A newer historical candidate is shown first. The existing prospective monitor remains active until you explicitly activate the candidate.</div>}
    {requiresRebootstrap && <div role="alert" className="border p-4" style={{ ...box, color: "#f2b84b" }}>{active.rebootstrapReason} Configure the replacement in Comparison settings below and select Preview comparison.</div>}
    {view === "history" && <div className="verdict-drift-grid"><section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>SUGGESTED FIRST CHECK</div>
      <h2 className="text-lg font-semibold mt-1">{traceCount > 0 ? "Verdict’s suggested comparison" : "Connect model-call evidence"}</h2>
      {traceCount > 0 ? <div className="divide-y mt-4 text-sm" style={{ borderColor: "#26332e" }}><div className="py-3"><strong>Start with all eligible calls</strong><p style={{ color: "#94a39d" }}>Use a broad comparison before splitting traffic into smaller groups.</p></div><div className="py-3"><strong>Watch recorded model-call changes</strong><p style={{ color: "#94a39d" }}>Compare provider errors, blank captured response text, and refusal-like language.</p></div><div className="py-3"><strong>Freeze the reviewed reference</strong><p style={{ color: "#94a39d" }}>Activation starts a new prospective cohort; the historical preview does not become a live alert.</p></div></div>
        : <p className="text-sm mt-3" style={{ color: "#94a39d" }}>{agentRunCount > 0 ? `${agentRunCount.toLocaleString()} agent runs are available for local analysis. Add genuine completed provider calls before previewing a model-call alert.` : "Connect an instrumented source before previewing a model-call alert."}</p>}
      {(traceCount > 0 || (logicalSession && agentRunCount > 0) || conversation) && <button type="button" disabled={!token || busy} onClick={previewComparison} className="border px-4 py-2 text-sm mt-4" style={{ borderColor: "#4ee1aa", color: "#4ee1aa" }}>{logicalSession || conversation ? "Preview descriptive comparison" : form.groupingMode === "none" ? "Preview all-call check" : "Preview selected call groups"}</button>}
      {traceCount === 0 && onSetup && <button type="button" onClick={onSetup} className="underline text-sm mt-4" style={{ color: "#4ee1aa" }}>Set up model-call capture</button>}
    </section><aside className="border p-5" style={box}>
      <h2 className="font-semibold">{candidate ? "Review this candidate" : active ? "Current policy" : "Before activation"}</h2>
      {candidate?.snapshot ? <div className="text-sm mt-3 space-y-2" style={{ color: "#94a39d" }}>
        <p>{candidate.snapshot.manifest.reference_unit_ids.length} historical reference calls · {candidate.snapshot.manifest.current_unit_ids.length} historical current calls.</p>
        <p>Reference floor: {candidate.policy.minimum_reference} eligible calls with a measured metric · {referenceReady ? "met" : "not met"}.</p>
        <p>New traffic target after activation: {candidate.policy.prospective_target} calls. Historical current calls do not count toward it.</p>
      </div> : active?.policy ? <p className="text-sm mt-3" style={{ color: "#94a39d" }}>The saved policy is collecting toward {active.policy.prospective_target} new calls. The comparison below shows actual progress and evidence coverage.</p>
        : <p className="text-sm mt-3" style={{ color: "#94a39d" }}>{traceCount ? "Preview a comparison to see its actual cohort counts and sample readiness." : "Capture genuine completed provider calls before previewing a statistical comparison."}</p>}
      <p className="text-xs mt-4" style={{ color: "#94a39d" }}>Destination: in-app Monitoring. A saved policy does not start the separate worker or configure external delivery.</p>
      {excludedTraceCount != null && <p className="text-xs mt-2" style={{ color: "#94a39d" }}>Monitor input: {traceCount.toLocaleString()} of {totalTraces.toLocaleString()} stored traces eligible · {excludedTraceCount.toLocaleString()} excluded. This is eligibility across the store, not the selected historical window.</p>}
      {onPlan && <button type="button" onClick={onPlan} className="underline text-sm mt-4" style={{ color: "#4ee1aa" }}>Review worker and delivery setup →</button>}
      {onGroups && <button type="button" onClick={onGroups} className="block underline text-sm mt-3" style={{ color: "#4ee1aa" }}>Review grouping options →</button>}
    </aside></div>}
    {view === "history" && traceCount > 0 && <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#94a39d" }}>OPTIONAL SETUP ASSIST</div>
      <EvidenceExplain root={root} scope="setup" onApplyDraft={(draft) => {
        if (draft.check === "capture_first") { onSetup?.(); return; }
        if (draft.check === "agent_runs") { onPlan?.(); return; }
        setForm((current) => ({ ...current, analysisUnit: "trace",
          groupingMode: draft.grouping === "provider_model" ? "provider_model" : "none",
          evaluatorFingerprint: "", dimension: "", labelKey: "" }));
        setCandidate(null); setDescriptive(null); setDraftApplied("monitor");
      }} />
      {traceCount >= 60 && <><p className="text-sm mt-4" style={{ color: "#94a39d" }}>For a model-call comparison, AI can suggest a historical split and sample floors from eligible-call counts. Review the proposal, then preview the actual cohorts before activation.</p>
        <EvidenceExplain root={root} scope="setup" draftKind="cohort" draftActionLabel="Use draft in comparison form" onApplyDraft={(draft) => {
          setForm((current) => ({ ...current, analysisUnit: "trace", windowMode: "count",
            groupingMode: draft.grouping, referenceRatio: draft.referenceShare / 100,
            minimumReference: draft.minimumReference, minimumCurrent: draft.minimumCurrent,
            prospectiveTarget: draft.prospectiveTarget, evaluatorFingerprint: "" }));
          setCandidate(null); setDescriptive(null); setDraftApplied("cohort");
        }} />
      </>}
      {draftApplied && <p role="status" className="text-sm mt-2" style={{ color: "#f2b84b" }}>{draftApplied === "cohort" ? "Cohort draft filled the local comparison form. Preview and review the actual eligible counts before activation." : "Setup draft applied to local form. Preview the comparison and review its cohorts before activation. Suggested interval is advisory; save a schedule separately."}</p>}
    </section>}
    {descriptive?.unit === "conversation" && <ConversationPreview preview={descriptive} onOpen={openConversation} />}
    {descriptive && descriptive.unit !== "conversation" && <LogicalSessionPreview preview={descriptive} />}
    {conversationDetail && <section className="border p-5 space-y-2" style={box}>
      <div className="flex justify-between"><h3 className="font-semibold">Conversation evidence</h3><button className="underline" onClick={() => setConversationDetail(null)}>Close</button></div>
      {conversationDetail.conversation.revision !== conversationDetail.expectedRevision
        ? <p role="alert" style={{ color: "#f2b84b" }}>This transcript changed since the comparison. Run the comparison again to inspect current evidence.</p>
        : conversationDetail.expectedState !== "ineligible" && detailGradeState(conversationDetail, descriptive.dimension) !== conversationDetail.expectedState
          ? <p role="alert" style={{ color: "#f2b84b" }}>This grade changed since the comparison. Run the comparison again to inspect current evidence.</p>
        : <><div className="text-xs">{conversationDetail.assessments.map((grade) => `${grade.status}: ${Object.entries(grade.dimensions).map(([name, result]) => `${name} ${result.state}`).join(", ")}`).join(" · ") || "No current grade"}</div>
          {conversationDetail.expectedReason && <p className="text-xs">Not evaluable: {conversationDetail.expectedReason.replaceAll("_", " ")}</p>}
          {conversationDetail.conversation.messages.map((message, index) => <div key={index} className="border p-3 text-sm" style={box}><strong>{index + 1} · {message.role}</strong><p dir="auto" className="whitespace-pre-wrap mt-1">{message.content}</p></div>)}</>}
    </section>}
    {candidate?.snapshot && <MonitorSnapshot response={candidate} evaluators={evaluators} fallbackTarget={form.prospectiveTarget} onOpenTrace={onOpenTrace} />}
    {view === "history" && candidate?.snapshot && <section className="border p-5" style={box}>
      <h2 className="font-semibold">Review before activating</h2>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>This historical preview used {candidate.snapshot.manifest.reference_unit_ids.length} reference and {candidate.snapshot.manifest.current_unit_ids.length} current calls. Activation freezes the reference and starts a new prospective cohort; it does not turn this historical result into an alert on new traffic.</p>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>Activation saves the policy for in-app Monitoring. Start and verify the separate worker in Suggested setup; configure any external alert delivery separately.</p>
      {!referenceReady && <p role="status" className="text-sm mt-3" style={{ color: "#f2b84b" }}>No reference metric within a cohort meets the reviewed sample floor. Capture more eligible calls, choose fewer groups, or adjust the floor and preview again.</p>}
      {referenceReady && candidate.snapshot.comparison?.status === "insufficient" && <p className="text-sm mt-3" style={{ color: "#f2b84b" }}>The historical current cohort could not produce a comparison. Activation starts a new prospective cohort; it will report no alert decision until enough new evidence arrives.</p>}
      <button type="button" disabled={busy || !referenceReady} onClick={async () => {
        const activated = await post("/api/monitor/activate", { policyId: candidate.policy.policy_id, expectedActivePolicyId: active?.policy?.policy_id || null });
        if (activated) { setActive(activated); setCandidate(null); onChanged?.(); }
      }} className="px-4 py-2 text-sm mt-4" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Activate reviewed monitor</button>
    </section>}
    {active?.snapshot && <MonitorSnapshot response={active} evaluators={evaluators} fallbackTarget={form.prospectiveTarget} onOpenTrace={onOpenTrace} />}
    {view === "history" && <details className="border" style={box}>
      <summary className="p-5 cursor-pointer"><span className="text-xs font-mono" style={{ color: "#4ee1aa" }}>ADVANCED COMPARISON SETTINGS</span><span className="block text-sm mt-1" style={{ color: "#94a39d" }}>{conversation ? "Choose historical windows, grade, and source label" : "Change cohorts, measurement, grouping, and sample floors before previewing"}</span></summary>
      <div className="px-5 pb-5 border-t" style={{ borderColor: "#26332e" }}>
      <h2 className="text-lg font-semibold mt-1">{conversation ? "Compare current conversation grades" : "Explore first, then activate one immutable monitor"}</h2>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>{conversation ? "Choose two historical date ranges in your local time, one exact whole-conversation binary grade, and an optional source label. Results show UTC dates. This view has no activation or alert." : "Membership is chosen from event time before metric outcomes are compared. No clustering is required. Preview is exploratory; only an activated policy can become authoritative."}</p>
      <div className="grid sm:grid-cols-2 gap-4 mt-5">
        {!conversation && <label className="text-sm">Window mode<select value={form.windowMode} onChange={(event) => update("windowMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="count">Count cohorts</option><option value="explicit">Explicit date ranges</option></select></label>}
        {form.windowMode === "count" && <label className="text-sm">Reference share<input type="number" min="0.5" max="0.95" step="0.05" value={form.referenceRatio} onChange={(event) => update("referenceRatio", Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>}
        <label className="text-sm">Analysis unit<select value={form.analysisUnit} onChange={(event) => update("analysisUnit", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="trace">Genuine model call</option><option value="logical_session">Logical session (descriptive)</option><option value="conversation">Conversation grade (descriptive)</option></select></label>
        {conversation ? <>
          <label className="text-sm">Evaluator fingerprint<input value={form.evaluatorFingerprint} onChange={(event) => update("evaluatorFingerprint", event.target.value)} placeholder="64-character fingerprint from Evaluator Lab" className="block w-full mt-1 border p-2 bg-transparent" /></label>
          <label className="text-sm">Binary rubric dimension<input value={form.dimension} onChange={(event) => update("dimension", event.target.value)} placeholder="e.g. quality" className="block w-full mt-1 border p-2 bg-transparent" /></label>
          <label className="text-sm">Source label key (optional)<input value={form.labelKey} onChange={(event) => update("labelKey", event.target.value)} placeholder="e.g. group" className="block w-full mt-1 border p-2 bg-transparent" /></label>
        </> : <>
          <label className="text-sm">Measurement<select value={form.evaluatorFingerprint} onChange={(event) => update("evaluatorFingerprint", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="">{logicalSession ? "Deterministic agent checks only" : "Deterministic trace checks only"}</option>{measurements.map((identity) => <option key={identity.fingerprint} value={identity.fingerprint}>{identity.label}</option>)}</select><span className="block text-xs mt-1" style={{ color: "#94a39d" }}>{form.evaluatorFingerprint ? "Compares existing stored judgments; this preview makes no judge calls." : logicalSession ? "Compares completed execution and final-output presence by logical session." : "Compares provider errors, blank captured response text, and refusal-like language."}</span>{logicalSession && <AgentEvaluatorDiscoveryNote truncated={agentEvaluatorDiscoveryTruncated} />}</label>
          <label className="text-sm">Comparison facet<select disabled={logicalSession} value={form.groupingMode} onChange={(event) => update("groupingMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="none">{logicalSession ? "No grouping for logical sessions" : "All eligible calls (recommended)"}</option>{!logicalSession && <><option value="provider_model">Provider and model</option><option value="cluster">Active reviewed cluster</option></>}</select></label>
        </>}
        {form.windowMode === "explicit" && ["referenceStart", "referenceEnd", "currentStart", "currentEnd"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="datetime-local" value={form[name]} onChange={(event) => update(name, event.target.value)} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
        {!logicalSession && !conversation && ["minimumReference", "minimumCurrent", "prospectiveTarget"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="number" min="1" value={form[name]} onChange={(event) => update(name, Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
      </div>
      <div className="flex flex-wrap gap-2 mt-5">
        <button disabled={!token || busy} onClick={previewComparison} className="border px-4 py-2 text-sm">{conversation ? "Run historical comparison" : "Preview comparison"}</button>
        {canRunActiveMonitor(active) && <button disabled={busy} onClick={async () => {
          const result = await post("/api/monitor/run"); if (result) { setActive(result); onChanged?.(); }
        }} className="border px-4 py-2 text-sm">Run next cohort now</button>}
      </div>
      </div>
    </details>}
    {active?.approvedHistoricalSnapshot && <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#f2b84b" }}>APPROVED HISTORICAL PREVIEW</div>
      <div className="font-semibold mt-1">Historical preview: {active.approvedHistoricalSnapshot.comparison.status.replaceAll("_", " ")}</div>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>This is the historical comparison used to approve the policy. Activation froze its reference cohort and opened a new prospective bucket; it did not reuse the historical current cohort as new traffic.</p>
      <div className="text-sm mt-3">{active.approvedHistoricalSnapshot.manifest.reference_unit_ids.length} historical reference → {active.approvedHistoricalSnapshot.manifest.current_unit_ids.length} historical current</div>
      <MonitorComparisonMetrics comparison={active.approvedHistoricalSnapshot.comparison}
        referenceSummary={active.approvedHistoricalSnapshot.manifest.reference_summary}
        currentSummary={active.approvedHistoricalSnapshot.manifest.current_summary}
        onOpenTrace={evidenceTraceOpener(onOpenTrace, evaluators, active.policy)} />
    </section>}
  </div>;
}
