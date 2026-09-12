import React, { useEffect, useState } from "react";
import { monitorRequest } from "./monitor-form.mjs";

const box = { borderColor: "#26332e", background: "#111715" };

export function monitorStateParts(monitor) {
  const legacy = monitor?.policy && monitor?.snapshot ? monitor : null;
  return {
    active: monitor?.active || (["active", "requires_rebootstrap"].includes(monitor?.state) ? legacy : null),
    candidate: monitor?.candidate || (monitor?.state === "candidate" ? legacy : null),
  };
}

const metricLabel = (metric) => {
  if (metric.startsWith("judge.") && metric.endsWith(".pass")) {
    return `${metric.slice(6, -5).replaceAll("_", " ")} pass rate`;
  }
  return ({
    provider_error: "Provider error rate",
    response_empty: "Empty-response rate",
    refusal_signature: "Refusal-language rate",
  })[metric] || metric.replaceAll("_", " ");
};

export function MonitorComparisonMetrics({ comparison }) {
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
  const cells = [...new Set([...metrics.keys(), ...coverage.keys()])];
  return <div className="mt-4 space-y-2">{cells.map((key) => {
    const metric = metrics.get(key);
    const evidence = coverage.get(key);
    const row = metric || evidence;
    const group = groups.get(row.group_id);
    return <div key={key} className="border p-3 text-sm" style={{ borderColor: "#26332e" }}>
      {row.group_id && <div className="text-xs mb-2" style={{ color: "#94a39d" }}>Group: <span title={row.group_id}>{group?.label || row.group_id}</span></div>}
      <span className="font-mono">{metricLabel(row.metric)}</span>
      {metric
        ? <span className="ml-3" style={{ color: metric.alert ? "#ff6b6b" : "#94a39d" }}>{(100 * metric.reference_value).toFixed(1)}% → {(100 * metric.current_value).toFixed(1)}% · effect {(100 * metric.effect).toFixed(1)}pp · adjusted p {metric.p_adjusted.toPrecision(3)} · eligible n {metric.reference_n} → {metric.current_n}</span>
        : <span className="ml-3" style={{ color: "#f2b84b" }}>No PASS/FAIL comparison yet</span>}
      {evidence && <div className="text-xs mt-2" style={{ color: "#94a39d" }}>Evidence: {evidence.reference_evaluable} → {evidence.current_evaluable} evaluable · {evidence.reference_unclear} → {evidence.current_unclear} unclear · {evidence.reference_missing} → {evidence.current_missing} not judged · {evidence.reference_error} → {evidence.current_error} judge errors</div>}
    </div>;
  })}</div>;
}

function MonitorSnapshot({ response, evaluators, fallbackTarget }) {
  const snapshot = response.snapshot;
  const manifest = snapshot.manifest;
  const comparison = snapshot.comparison;
  const policy = response.policy;
  const candidate = response.policyState === "candidate" || response.state === "candidate";
  const measurement = evaluators.find(
    (identity) => identity.fingerprint === policy?.evaluator_fingerprint,
  );
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
  return <section className="border p-5" style={box}>
    <div className="flex flex-wrap gap-3 items-center justify-between">
      <div><div className="text-xs font-mono" style={{ color: candidate ? "#f2b84b" : "#4ee1aa" }}>{candidate ? "EXPLORATORY HISTORICAL COMPARISON" : "ACTIVE PROSPECTIVE MONITOR"}</div><div className="font-semibold mt-1">{label}</div></div>
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
    <MonitorComparisonMetrics comparison={comparison} />
    {comparison.status === "insufficient" && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{awaitingEvaluator ? "Run the selected evaluator, then run this monitor again. To stop measuring that evaluator, preview and activate a replacement monitor." : collecting ? "No statistical test was run because the prospective bucket is still collecting." : "The bucket closed, but no metric met its configured eligible-unit minimums; no alert/no-alert conclusion was produced."}</p>}
    {comparison.unseen_group_share > 0 && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{comparison.status === "reference_stale" ? "Comparison suspended" : "Coverage note"}: {(100 * comparison.unseen_group_share).toFixed(1)}% of current traces are outside the frozen {policy?.grouping_mode === "cluster" ? "cluster" : "provider/model"} reference{comparison.unassigned_group_share > 0 ? ` (${(100 * comparison.unassigned_group_share).toFixed(1)}% are unassigned)` : ""}.{comparison.status === "reference_stale" ? " Review the policy before creating a new candidate; Verdict did not silently rebase it." : " These traces were excluded from like-for-like metric tests."}</p>}
  </section>;
}

export function Monitor({ configUrl, evaluation = {}, initialState = null, view = "history", onChanged = null }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const evaluators = (evaluation.availableIdentities || []).filter(
    (identity, index, rows) => identity.complete && identity.fingerprint
      && rows.findIndex((other) => other.fingerprint === identity.fingerprint) === index,
  );
  const [token, setToken] = useState(null);
  const [active, setActive] = useState(initialState?.active || null);
  const [candidate, setCandidate] = useState(initialState?.candidate || null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    windowMode: "count", referenceRatio: 0.8, minimumReference: 30,
    minimumCurrent: 30, prospectiveTarget: 30, minimumEffect: 0.1,
    analysisUnit: "session", groupingMode: "none",
    evaluatorFingerprint: "",
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

  const update = (name, value) => {
    setCandidate(null);
    setForm((current) => ({ ...current, [name]: value }));
  };
  const requiresRebootstrap = active?.state === "requires_rebootstrap";
  return <div className="max-w-5xl space-y-4">
    {view === "history" && <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>POLICY LIFECYCLE</div>
      <h2 className="text-lg font-semibold mt-1">Explore first, then activate one immutable monitor</h2>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>Each session or run contributes one result. Event time orders historical cohorts; stored ingestion order separates traffic seen before and after activation. Preview is exploratory; only an activated policy can become authoritative.</p>
      <div className="grid sm:grid-cols-2 gap-4 mt-5">
        <label className="text-sm">Window mode<select value={form.windowMode} onChange={(event) => update("windowMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="count">Count cohorts</option><option value="explicit">Explicit date ranges</option></select></label>
        {form.windowMode === "count" && <label className="text-sm">Reference share<input type="number" min="0.5" max="0.95" step="0.05" value={form.referenceRatio} onChange={(event) => update("referenceRatio", Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>}
        <label className="text-sm">Analysis unit<select value={form.analysisUnit} onChange={(event) => update("analysisUnit", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="session">Session (recommended)</option><option value="run">Agent run</option></select></label>
        <label className="text-sm">Measurement<select value={form.evaluatorFingerprint} onChange={(event) => update("evaluatorFingerprint", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="">Deterministic trace checks only</option>{evaluators.map((identity) => <option key={identity.fingerprint} value={identity.fingerprint}>{identity.label}</option>)}</select><span className="block text-xs mt-1" style={{ color: "#94a39d" }}>{form.evaluatorFingerprint ? "Compares existing stored judgments; this monitor makes no judge calls." : "Compares provider errors, empty responses, and refusal-like language."}</span></label>
        <label className="text-sm">Comparison facet<select value={form.groupingMode} onChange={(event) => update("groupingMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="none">All eligible calls (recommended)</option><option value="provider_model">Provider and model</option><option value="cluster">Active reviewed cluster</option></select></label>
        {form.windowMode === "explicit" && ["referenceStart", "referenceEnd", "currentStart", "currentEnd"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="datetime-local" value={form[name]} onChange={(event) => update(name, event.target.value)} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
        {["minimumReference", "minimumCurrent", "prospectiveTarget"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="number" min="1" value={form[name]} onChange={(event) => update(name, Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
      </div>
      <div className="flex flex-wrap gap-2 mt-5">
        <button disabled={!token || busy} onClick={async () => { const result = await post("/api/monitor/preview", monitorRequest(form)); if (result) { setCandidate(result); onChanged?.(); } }} className="border px-4 py-2 text-sm">Preview comparison</button>
        {candidate && <button disabled={busy} onClick={async () => {
          const activated = await post("/api/monitor/activate", { policyId: candidate.policy.policy_id, expectedActivePolicyId: active?.policy?.policy_id || null });
          if (activated) { setActive(activated); setCandidate(null); onChanged?.(); }
        }} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Activate monitor</button>}
        {active?.state === "active" && <button disabled={busy} onClick={async () => {
          const result = await post("/api/monitor/run"); if (result) { setActive(result); onChanged?.(); }
        }} className="border px-4 py-2 text-sm">Run next cohort now</button>}
      </div>
    </section>}
    {view === "status" && !active?.snapshot && !candidate?.snapshot && <section className="border p-5" style={box}><div className="text-xs font-mono" style={{ color: "#f2b84b" }}>MONITORING</div><h2 className="text-lg font-semibold mt-1">No comparison configured</h2><p className="text-sm mt-2" style={{ color: "#94a39d" }}>Open Compare History to create a historical comparison. Activate it only if new traffic will continue arriving.</p></section>}
    {error && <div role="alert" className="border p-4" style={{ ...box, color: "#ff6b6b" }}>{error}</div>}
    {candidate && active && <div role="status" className="border p-4 text-sm" style={{ ...box, color: "#f2b84b" }}>A newer historical candidate is shown below. The existing prospective monitor remains active until you explicitly activate the candidate.</div>}
    {requiresRebootstrap && <div role="alert" className="border p-4" style={{ ...box, color: "#f2b84b" }}>{active.rebootstrapReason} Configure the replacement above and select Preview comparison.</div>}
    {active?.snapshot && <MonitorSnapshot response={active} evaluators={evaluators} fallbackTarget={form.prospectiveTarget} />}
    {candidate?.snapshot && <MonitorSnapshot response={candidate} evaluators={evaluators} fallbackTarget={form.prospectiveTarget} />}
    {active?.approvedHistoricalSnapshot && <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#f2b84b" }}>APPROVED HISTORICAL PREVIEW</div>
      <div className="font-semibold mt-1">{active.approvedHistoricalSnapshot.comparison.status.replaceAll("_", " ")}</div>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>This is the historical comparison used to approve the policy. Activation froze its reference cohort and opened a new prospective bucket; it did not reuse the historical current cohort as new traffic.</p>
      <div className="text-sm mt-3">{active.approvedHistoricalSnapshot.manifest.reference_unit_ids.length} historical reference → {active.approvedHistoricalSnapshot.manifest.current_unit_ids.length} historical current</div>
      <MonitorComparisonMetrics comparison={active.approvedHistoricalSnapshot.comparison} />
    </section>}
  </div>;
}
