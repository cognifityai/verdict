import React, { useRef, useState, useEffect } from "react";
import { AlertTriangle, LoaderCircle } from "lucide-react";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };
const MAX_BYTES = 4 * 1024 * 1024;
const FORMATS = [["auto", "Auto-detect"], ["chatgpt", "ChatGPT"], ["claude_ai", "Claude.ai"], ["cowork", "Cowork JSONL"], ["openai_jsonl", "OpenAI JSON / JSONL"]];

export function InspectLab({ configUrl }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const [token, setToken] = useState(null);
  const [available, setAvailable] = useState(null);
  const [content, setContent] = useState("");
  const [format, setFormat] = useState("auto");
  const [semantic, setSemantic] = useState(false);
  const [judge, setJudge] = useState(false);
  const [judgeModel, setJudgeModel] = useState("claude-haiku-4-5-20251001");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const fileRead = useRef(0);
  const requestInFlight = useRef(false);

  useEffect(() => {
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }),
      fetch(`${root}/api/evaluators`, { credentials: "same-origin" }),
    ]).then(async ([auth, environment]) => {
      if (!auth.ok || !environment.ok) throw new Error("Inspect is unavailable.");
      setToken((await auth.json()).setupToken);
      setAvailable((await environment.json()).inspectPackageAvailable === true);
    }).catch((failure) => setError(String(failure)));
  }, [root]);

  const contentBytes = new Blob([content]).size;
  async function chooseFile(event) {
    const generation = ++fileRead.current;
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_BYTES) { setError("The export exceeds the 4 MiB limit."); return; }
    try {
      const text = await file.text();
      if (generation === fileRead.current) { setContent(text); setResult(null); setError(null); }
    } catch { if (generation === fileRead.current) setError("The export could not be read."); }
  }

  async function analyze() {
    if (requestInFlight.current) return;
    requestInFlight.current = true; setBusy(true); setError(null); setResult(null);
    const params = new URLSearchParams({
      format, semantic: semantic ? "1" : "0", judge: judge ? "1" : "0",
      judge_model: judgeModel,
      confirm_external_egress: judge && confirmed ? "1" : "0",
    });
    try {
      const response = await fetch(`${root}/api/evaluators/inspect?${params}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "text/plain;charset=UTF-8", "X-Verdict-Setup": token },
        body: content,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setResult(data);
    } catch (failure) { setError(String(failure)); }
    finally { requestInFlight.current = false; setBusy(false); }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(result.report, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = "verdict-inspect-report.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  const report = result?.report;
  const canRun = token && available && contentBytes > 0 && contentBytes <= MAX_BYTES
    && (!judge || confirmed) && !busy;
  return <div className="max-w-5xl space-y-4">
    <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="text-xs font-mono" style={{ color: C.green }}>ONE-OFF INSPECT</div>
      <h2 className="text-lg font-semibold mt-1">Analyze a conversation export</h2>
      <p className="text-sm mt-2" style={{ color: C.sub }}>Upload or paste JSON/JSONL to this Verdict server for a one-off analysis. Verdict does not add the export or its report to the store.</p>
      <div className="grid sm:grid-cols-2 gap-4 mt-5">
        <label className="text-sm">Export file<input aria-label="Conversation export file" type="file" accept=".json,.jsonl,application/json" onChange={chooseFile} className="block w-full border p-2 mt-1" /></label>
        <label className="text-sm">Format<select value={format} onChange={(event) => setFormat(event.target.value)} className="block w-full border p-2 mt-1 bg-transparent">{FORMATS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      <label className="block text-sm mt-4">Or paste JSON / JSONL<textarea aria-label="Conversation export JSON" value={content} onChange={(event) => { fileRead.current += 1; setContent(event.target.value); setResult(null); }} rows={10} spellCheck={false} className="block w-full border p-3 mt-1 bg-transparent font-mono text-xs" /></label>
      <div className="text-xs mt-1" style={{ color: contentBytes > MAX_BYTES ? C.red : C.faint }}>{contentBytes.toLocaleString()} / {MAX_BYTES.toLocaleString()} bytes</div>
      <div className="space-y-3 mt-5">
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={semantic} onChange={(event) => setSemantic(event.target.checked)} />Run semantic drift on the Verdict host; use the lexical fallback when the local model is unavailable.</label>
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={judge} onChange={(event) => { setJudge(event.target.checked); setConfirmed(false); }} />Also sample with the Anthropic judge (up to 75 calls).</label>
        {judge && <div className="border p-4" style={{ borderColor: C.amber }}><label className="text-sm">Judge model<input value={judgeModel} onChange={(event) => { setJudgeModel(event.target.value); setConfirmed(false); }} className="block w-full border p-2 mt-1 bg-transparent" /></label><label className="flex gap-2 text-sm mt-3"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I approve sending sampled user and assistant text to Anthropic. Provider billing is authoritative.</label></div>}
      </div>
      <button disabled={!canRun} onClick={analyze} className="mt-5 px-4 py-2 text-sm inline-flex items-center gap-2" style={{ background: canRun ? C.green : C.border, color: canRun ? "#0b0e0d" : C.faint }}>{busy && <LoaderCircle size={15} className="animate-spin" />}{busy ? "Analyzing…" : "Analyze export"}</button>
      {available === false && <div className="border p-4 mt-4 text-sm" style={{ borderColor: C.amber, color: C.amber }}>Install <code>cognifity-verdict-inspect</code> alongside Verdict to enable this view.</div>}
      {error && <div role="alert" className="border p-4 mt-4 flex gap-2 text-sm" style={{ borderColor: C.red, color: C.red }}><AlertTriangle size={15} />{error}</div>}
    </section>
    {report && <section className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
      <div className="flex flex-wrap justify-between gap-3"><div><div className="text-xs font-mono" style={{ color: C.green }}>INSPECT RESULT</div><h2 className="font-semibold mt-1">{report.format_name} export</h2></div><button onClick={download} className="border px-3 py-2 text-sm" style={{ borderColor: C.green, color: C.green }}>Download JSON</button></div>
      <div className="grid sm:grid-cols-3 gap-3 mt-4"><Metric label="Conversations" value={report.n_conversations} /><Metric label="Assistant turns" value={report.n_turns_total} /><Metric label="Substantive turns" value={report.n_turns_substantive} /></div>
      {report.notes?.length > 0 && <ul className="mt-4 text-sm space-y-1" style={{ color: C.amber }}>{report.notes.map((note) => <li key={note}>• {note}</li>)}</ul>}
      <ResultTable title="Structural metrics" rows={report.structural} columns={[["window", "Window"], ["n_turns", "Turns"], ["mean_words", "Mean words"], ["hedge_density", "Hedges"], ["refusal_rate", "Refusals"], ["apology_rate", "Apologies"]]} />
      <ResultTable title="Semantic drift" rows={report.semantic_drift} columns={[["comparison", "Comparison"], ["triggered", "Drift"], ["centroid_distance", "Centroid distance"], ["p_value", "p-value"], ["cluster_psi", "PSI"]]} />
      {report.judge?.length > 0 && <div className="mt-5"><h3 className="font-semibold">Judge sample</h3><pre className="border p-3 mt-2 overflow-x-auto text-xs" style={{ borderColor: C.border }}>{JSON.stringify(report.judge, null, 2)}</pre></div>}
    </section>}
  </div>;
}

function Metric({ label, value }) { return <div className="border p-3" style={{ borderColor: C.border }}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-xl font-semibold mt-1">{value}</div></div>; }
function ResultTable({ title, rows, columns }) { if (!rows?.length) return null; return <div className="mt-5 overflow-x-auto"><h3 className="font-semibold mb-2">{title}</h3><table className="w-full text-sm"><thead><tr>{columns.map(([, label]) => <th key={label} className="text-left border-b p-2" style={{ borderColor: C.border, color: C.faint }}>{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row.window || row.comparison || index}>{columns.map(([key]) => <td key={key} className="border-b p-2" style={{ borderColor: C.border }}>{display(row[key])}</td>)}</tr>)}</tbody></table></div>; }
function display(value) { if (typeof value === "boolean") return value ? "Yes" : "No"; if (typeof value === "number") return Number.isInteger(value) ? value : value.toFixed(4); return value ?? "—"; }
