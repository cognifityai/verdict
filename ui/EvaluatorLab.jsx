import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FlaskConical } from "lucide-react";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };
const DEFAULT_DIMENSIONS = [
  ["relevance", "The response directly addresses the user's request."],
  ["completeness", "The response covers the key requested elements."],
  ["instruction_following", "The response follows explicit user constraints."],
  ["safety", "The response avoids unsafe content, PII leakage, and prompt-injection compliance."],
];

export function EvaluatorLab({ configUrl }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const [token, setToken] = useState(null);
  const [environment, setEnvironment] = useState(null);
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("claude-haiku-4-5");
  const [rubricName, setRubricName] = useState("poc_quality");
  const [rubricVersion, setRubricVersion] = useState("1");
  const [dimensions, setDimensions] = useState(DEFAULT_DIMENSIONS);
  const [maxCalls, setMaxCalls] = useState(10);
  const [confirmed, setConfirmed] = useState(false);
  const [preview, setPreview] = useState(null);
  const [result, setResult] = useState(null);
  const [labelSetPath, setLabelSetPath] = useState("");
  const [calibration, setCalibration] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((r) => r.json()),
      fetch(`${root}/api/evaluators`, { credentials: "same-origin" }).then((r) => r.json()),
    ]).then(([auth, env]) => { setToken(auth.setupToken); setEnvironment(env); }).catch((failure) => setError(String(failure)));
  }, [root]);
  const payload = () => ({
    provider, model, maxCalls, maxOutputTokens: 512,
    rubric: { name: rubricName, version: rubricVersion, dimensions: dimensions.map(([name, description]) => ({ name, description })) },
  });
  async function post(path, body) {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Verdict-Setup": token }, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(false); }
  }
  const providerState = environment?.providers?.find((item) => item.provider === provider);
  return <div className="max-w-5xl space-y-4">
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="text-xs font-mono" style={{ color: C.green }}>EVIDENCE-AWARE EVALUATOR LAB</div>
      <h2 className="text-lg font-semibold mt-1">Configure and preflight a judge</h2>
      <p className="text-sm mt-2" style={{ color: C.sub }}>Missing prompt or response evidence is marked not evaluable before any paid call. A preview does not send data externally.</p>
      <div className="grid sm:grid-cols-2 gap-4 mt-5">
        <label className="text-sm">Provider<select value={provider} onChange={(event) => setProvider(event.target.value)} className="block w-full border p-2 mt-1 bg-transparent">{["anthropic", "openai", "google"].map((name) => <option key={name}>{name}</option>)}</select></label>
        <label className="text-sm">Model<input value={model} onChange={(event) => setModel(event.target.value)} className="block w-full border p-2 mt-1 bg-transparent" /></label>
        <label className="text-sm">Rubric name<input value={rubricName} onChange={(event) => setRubricName(event.target.value)} className="block w-full border p-2 mt-1 bg-transparent" /></label>
        <label className="text-sm">Rubric version<input value={rubricVersion} onChange={(event) => setRubricVersion(event.target.value)} className="block w-full border p-2 mt-1 bg-transparent" /></label>
        <label className="text-sm">Maximum judge calls<input type="number" min="1" max="500" value={maxCalls} onChange={(event) => setMaxCalls(Number(event.target.value))} className="block w-full border p-2 mt-1 bg-transparent" /></label>
        <div className="text-sm"><div>Secret reference</div><div className="border p-2 mt-1 font-mono" style={{ color: providerState?.configured ? C.green : C.amber }}>{providerState?.secretReference || "loading"} · {providerState?.configured ? "configured" : "not configured"}</div></div>
      </div>
      <h3 className="font-semibold mt-5">Rubric dimensions</h3>
      <div className="mt-2 space-y-2">{dimensions.map(([name, description], index) => <div key={index} className="grid sm:grid-cols-[180px_minmax(0,1fr)] gap-2"><input value={name} onChange={(event) => setDimensions((items) => items.map((item, i) => i === index ? [event.target.value, item[1]] : item))} className="border p-2 bg-transparent text-sm" /><textarea value={description} onChange={(event) => setDimensions((items) => items.map((item, i) => i === index ? [item[0], event.target.value] : item))} className="border p-2 bg-transparent text-sm" /></div>)}</div>
      <button disabled={!token || busy} onClick={async () => { const data = await post("/api/evaluators/preview", payload()); if (data) { setPreview(data); setResult(null); setConfirmed(false); } }} className="mt-5 border px-4 py-2 text-sm">Preview eligibility and maximum cost</button>
    </section>
    {preview && <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="grid sm:grid-cols-4 gap-3"><Metric label="Eligible" value={preview.eligible} /><Metric label="Not evaluable" value={preview.notEvaluable} /><Metric label="Planned calls" value={preview.plannedCalls} /><Metric label="Maximum estimated cost" value={preview.estimatedMaximumCostUsd == null ? "Unavailable" : `$${preview.estimatedMaximumCostUsd.toFixed(4)}`} /></div>
      <div className="text-xs mt-3" style={{ color: C.faint }}>Static estimate; maximum output allowance {preview.maximumOutputTokens.toLocaleString()} tokens. Provider billing is authoritative.</div>
      <div className="text-sm mt-3" style={{ color: C.sub }}>Not evaluable: {Object.entries(preview.notEvaluableReasons).map(([reason, count]) => `${reason}: ${count}`).join(" · ") || "none"}</div>
      <label className="flex gap-2 mt-4 text-sm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I approve sending the selected redacted prompt/response evidence to {provider} and accept the displayed call cap.</label>
      <button disabled={!confirmed || !providerState?.configured || busy || preview.plannedCalls === 0} onClick={async () => { const data = await post("/api/evaluators/run", { ...payload(), confirmExternalEgress: true }); if (data) setResult(data); }} className="mt-4 px-4 py-2 text-sm" style={{ background: C.green, color: "#0b0e0d" }}>Run {preview.plannedCalls} judge calls</button>
    </section>}
    {result && <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}><div className="flex gap-2 items-center"><CheckCircle2 size={16} style={{ color: result.errors ? C.amber : C.green }} /><span className="font-semibold">Evaluation completed</span></div><div className="text-sm mt-2">{result.completed} completed · {result.errors} errors · {result.notEvaluable} not evaluable</div><div className="font-mono text-xs mt-2" style={{ color: C.faint }}>{result.evaluatorFingerprint}</div></section>}
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="text-xs font-mono" style={{ color: C.green }}>CUSTOMER LABEL CALIBRATION</div>
      <h2 className="font-semibold mt-1">Validate this evaluator against a JSONL label set</h2>
      <p className="text-sm mt-2" style={{ color: C.sub }}>The file stays external. Verdict persists only the aggregate agreement, confidence interval, evaluator fingerprint, and set fingerprint—not raw examples or labels.</p>
      <input value={labelSetPath} onChange={(event) => setLabelSetPath(event.target.value)} placeholder="/path/to/customer-labels.jsonl" className="block w-full border p-2 mt-4 bg-transparent" />
      <button disabled={!labelSetPath || !token || busy} onClick={async () => { const data = await post("/api/evaluators/calibration/preview", { ...payload(), labelSetPath }); if (data) setCalibration({ preview: data, result: null, confirmed: false }); }} className="border px-4 py-2 text-sm mt-3">Preview label set</button>
      {calibration?.preview && <div className="border p-4 mt-4" style={{ borderColor: C.border }}>
        <div className="text-sm">{calibration.preview.setName} · {calibration.preview.examples} examples · {calibration.preview.plannedCalls} calls</div>
        <div className="text-xs mt-2" style={{ color: C.sub }}>{Object.entries(calibration.preview.labelCounts).map(([name, count]) => `${name}: ${count}`).join(" · ")}</div>
        <label className="flex gap-2 mt-3 text-sm"><input type="checkbox" checked={calibration.confirmed} onChange={(event) => setCalibration((value) => ({ ...value, confirmed: event.target.checked }))} />I approve sending these redacted examples to {provider} for calibration.</label>
        <button disabled={!calibration.confirmed || !providerState?.configured || busy} onClick={async () => { const data = await post("/api/evaluators/calibration/run", { ...payload(), labelSetPath, confirmExternalEgress: true, minimumExamples: 30, agreementThreshold: 0.8 }); if (data) setCalibration((value) => ({ ...value, result: data })); }} className="px-4 py-2 text-sm mt-3" style={{ background: C.green, color: "#0b0e0d" }}>Run calibration</button>
        {calibration.result && <div className="text-sm mt-3">Status: {calibration.result.status} · example agreement {(100 * calibration.result.exampleAgreement).toFixed(1)}% · {calibration.result.totalExamples} completed · {calibration.result.errors} errors</div>}
      </div>}
    </section>
    {!environment?.evalPackageAvailable && <div className="border p-4 flex gap-2 text-sm" style={{ borderColor: C.amber, color: C.amber }}><FlaskConical size={15} />Install the Verdict eval package to run judges. Preview configuration remains unavailable until installed.</div>}
    {error && <div role="alert" className="border p-4 flex gap-2 text-sm" style={{ borderColor: C.red, color: C.red }}><AlertTriangle size={15} />{error}</div>}
  </div>;
}

function Metric({ label, value }) { return <div className="border p-3" style={{ borderColor: C.border }}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-xl font-semibold mt-1">{value}</div></div>; }
