import React, { useEffect, useState } from "react";
import { monitorRequest } from "./monitor-form.mjs";

const box = { borderColor: "#26332e", background: "#111715" };

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
  const coverage = new Map(
    (comparison?.metric_coverage || []).map((item) => [item.metric, item]),
  );
  const metrics = new Map((comparison?.metrics || []).map((item) => [item.metric, item]));
  const names = [...new Set([...metrics.keys(), ...coverage.keys()])];
  return <div className="mt-4 space-y-2">{names.map((name) => {
    const metric = metrics.get(name);
    const evidence = coverage.get(name);
    return <div key={name} className="border p-3 text-sm" style={{ borderColor: "#26332e" }}>
      <span className="font-mono">{metricLabel(name)}</span>
      {metric
        ? <span className="ml-3" style={{ color: metric.alert ? "#ff6b6b" : "#94a39d" }}>{(100 * metric.reference_value).toFixed(1)}% → {(100 * metric.current_value).toFixed(1)}% · effect {(100 * metric.effect).toFixed(1)}pp · adjusted p {metric.p_adjusted.toPrecision(3)} · eligible n {metric.reference_n} → {metric.current_n}</span>
        : <span className="ml-3" style={{ color: "#f2b84b" }}>No PASS/FAIL comparison yet</span>}
      {evidence && <div className="text-xs mt-2" style={{ color: "#94a39d" }}>Evidence: {evidence.reference_evaluable} → {evidence.current_evaluable} evaluable · {evidence.reference_unclear} → {evidence.current_unclear} unclear · {evidence.reference_missing} → {evidence.current_missing} not judged · {evidence.reference_error} → {evidence.current_error} judge errors</div>}
    </div>;
  })}</div>;
}

export function Monitor({ configUrl, evaluation = {} }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const evaluators = (evaluation.availableIdentities || []).filter(
    (identity, index, rows) => identity.complete && identity.fingerprint
      && rows.findIndex((other) => other.fingerprint === identity.fingerprint) === index,
  );
  const defaultEvaluator = evaluation.selectedIdentity?.complete
    && evaluation.selectedIdentity.fingerprint
    ? evaluation.selectedIdentity.fingerprint : "";
  const [token, setToken] = useState(null);
  const [active, setActive] = useState(null);
  const [candidate, setCandidate] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    windowMode: "count", referenceRatio: 0.8, minimumReference: 30,
    minimumCurrent: 30, prospectiveTarget: 30, minimumEffect: 0.1,
    analysisUnit: "trace", groupingMode: "none",
    evaluatorFingerprint: defaultEvaluator,
    referenceStart: "", referenceEnd: "", currentStart: "", currentEnd: "",
  });
  useEffect(() => {
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((response) => response.json()),
      fetch(`${root}/api/monitor`, { credentials: "same-origin" }).then((response) => response.json()),
    ]).then(([config, monitor]) => { setToken(config.setupToken); setActive(monitor); })
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
  const snapshot = candidate?.snapshot || active?.snapshot;
  const manifest = snapshot?.manifest;
  const comparison = snapshot?.comparison;
  const displayedPolicy = candidate?.policy || active?.policy;
  const selectedMeasurement = evaluators.find(
    (identity) => identity.fingerprint === displayedPolicy?.evaluator_fingerprint,
  );
  const collecting = manifest?.prospective_open === true;
  const comparisonLabel = collecting
    ? `Collecting ${manifest.current_unit_ids.length}/${active?.policy?.prospective_target || candidate?.policy?.prospective_target || form.prospectiveTarget}`
    : comparison?.status === "insufficient" ? "Insufficient evidence"
      : comparison?.status?.replaceAll("_", " ");
  return <div className="max-w-5xl space-y-4">
    <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>POLICY LIFECYCLE</div>
      <h2 className="text-lg font-semibold mt-1">Explore first, then activate one immutable monitor</h2>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>Membership is chosen from event time before metric outcomes are compared. No clustering is required. Preview is exploratory; only an activated policy can become authoritative.</p>
      <div className="grid sm:grid-cols-2 gap-4 mt-5">
        <label className="text-sm">Window mode<select value={form.windowMode} onChange={(event) => update("windowMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="count">Count cohorts</option><option value="explicit">Explicit date ranges</option></select></label>
        {form.windowMode === "count" && <label className="text-sm">Reference share<input type="number" min="0.5" max="0.95" step="0.05" value={form.referenceRatio} onChange={(event) => update("referenceRatio", Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>}
        <label className="text-sm">Analysis unit<select value={form.analysisUnit} onChange={(event) => update("analysisUnit", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="trace">Genuine model call</option></select></label>
        <label className="text-sm">Measurement<select value={form.evaluatorFingerprint} onChange={(event) => update("evaluatorFingerprint", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="">Deterministic trace checks only</option>{evaluators.map((identity) => <option key={identity.fingerprint} value={identity.fingerprint}>{identity.label}</option>)}</select><span className="block text-xs mt-1" style={{ color: "#94a39d" }}>{form.evaluatorFingerprint ? "Compares existing stored judgments; this monitor makes no judge calls." : "Compares provider errors, empty responses, and refusal-like language."}</span></label>
        <label className="text-sm">Comparison facet<select value={form.groupingMode} onChange={(event) => update("groupingMode", event.target.value)} className="block w-full mt-1 border p-2 bg-transparent"><option value="none">All eligible calls (recommended)</option><option value="provider_model">Provider and model</option><option value="cluster">Active reviewed cluster</option></select></label>
        {form.windowMode === "explicit" && ["referenceStart", "referenceEnd", "currentStart", "currentEnd"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="datetime-local" value={form[name]} onChange={(event) => update(name, event.target.value)} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
        {["minimumReference", "minimumCurrent", "prospectiveTarget"].map((name) => <label key={name} className="text-sm">{name.replace(/([A-Z])/g, " $1")}<input type="number" min="1" value={form[name]} onChange={(event) => update(name, Number(event.target.value))} className="block w-full mt-1 border p-2 bg-transparent" /></label>)}
      </div>
      <div className="flex flex-wrap gap-2 mt-5">
        <button disabled={!token || busy} onClick={async () => setCandidate(await post("/api/monitor/preview", monitorRequest(form)))} className="border px-4 py-2 text-sm">Preview comparison</button>
        {candidate && <button disabled={busy} onClick={async () => {
          const activated = await post("/api/monitor/activate", { policyId: candidate.policy.policy_id, expectedActivePolicyId: active?.policy?.policy_id || null });
          if (activated) { setActive(activated); setCandidate(null); }
        }} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Activate monitor</button>}
        {active?.state === "active" && <button disabled={busy} onClick={async () => {
          const result = await post("/api/monitor/run"); if (result) setActive(result);
        }} className="border px-4 py-2 text-sm">Run next cohort now</button>}
      </div>
    </section>
    {error && <div role="alert" className="border p-4" style={{ ...box, color: "#ff6b6b" }}>{error}</div>}
    {snapshot && <section className="border p-5" style={box}>
      <div className="flex flex-wrap gap-3 items-center justify-between">
        <div><div className="text-xs font-mono" style={{ color: candidate ? "#f2b84b" : "#4ee1aa" }}>{candidate ? "EXPLORATORY HISTORICAL COMPARISON" : "ACTIVE PROSPECTIVE MONITOR"}</div><div className="font-semibold mt-1">{comparisonLabel}</div></div>
        <div className="text-sm" style={{ color: "#94a39d" }}>{manifest.reference_unit_ids.length} reference → {manifest.current_unit_ids.length} current</div>
      </div>
      <div className="mt-4 h-8 flex overflow-hidden border" style={{ borderColor: "#26332e" }}><div style={{ width: `${100 * manifest.reference_unit_ids.length / Math.max(1, manifest.reference_unit_ids.length + manifest.current_unit_ids.length)}%`, background: "#1f5f4b" }} /><div className="flex-1" style={{ background: "#295a78" }} /></div>
      <div className="mt-3 text-xs" style={{ color: "#94a39d" }}>
        {collecting ? `Prospective bucket ${manifest.current_unit_ids.length}/${active?.policy?.prospective_target || candidate?.policy?.prospective_target || form.prospectiveTarget}; no comparison or alert decision has run.` : `Completed comparison look ${manifest.comparison_index} · alert threshold ${comparison.alpha_threshold.toPrecision(3)} · ${active?.policy?.sequential_method || candidate?.policy?.sequential_method}`}
      </div>
      <div className="mt-2 text-xs" style={{ color: "#94a39d" }}>Measurement: {displayedPolicy?.evaluator_fingerprint ? (selectedMeasurement?.label || `stored evaluator ${displayedPolicy.evaluator_fingerprint.slice(0, 8)}`) : "deterministic trace checks only"}</div>
      <MonitorComparisonMetrics comparison={comparison} />
      {comparison.status === "insufficient" && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>{collecting ? "No statistical test was run because the prospective bucket is still collecting." : "The bucket closed, but no metric met its configured eligible-unit minimums; no alert/no-alert conclusion was produced."}</p>}
      {comparison.status === "reference_stale" && <p className="text-sm mt-4" style={{ color: "#f2b84b" }}>Comparison suspended: {(100 * comparison.unseen_group_share).toFixed(1)}% of current traces use provider/model groups absent from the frozen reference. Review the policy before creating a new candidate; Verdict did not silently rebase it.</p>}
    </section>}
    {!candidate && active?.approvedHistoricalSnapshot && <section className="border p-5" style={box}>
      <div className="text-xs font-mono" style={{ color: "#f2b84b" }}>APPROVED HISTORICAL PREVIEW</div>
      <div className="font-semibold mt-1">{active.approvedHistoricalSnapshot.comparison.status.replaceAll("_", " ")}</div>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>This is the historical comparison used to approve the policy. Activation froze its reference cohort and opened a new prospective bucket; it did not reuse the historical current cohort as new traffic.</p>
      <div className="text-sm mt-3">{active.approvedHistoricalSnapshot.manifest.reference_unit_ids.length} historical reference → {active.approvedHistoricalSnapshot.manifest.current_unit_ids.length} historical current</div>
    </section>}
  </div>;
}
