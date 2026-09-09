import React, { useEffect, useState } from "react";
import { observedSourcePresentation, setupFailureMessage } from "./source-state.mjs";

const panel = "border p-5";
const style = { borderColor: "#26332e", background: "#111715" };

export function SetupWizard({ configUrl, onComplete, onNavigate, onRefresh, agentSummary = {} }) {
  const [token, setToken] = useState(null);
  const [source, setSource] = useState("local");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [claudeRoot, setClaudeRoot] = useState("~/.claude/projects");
  const [codexRoot, setCodexRoot] = useState("~/.codex/sessions");
  const [filePath, setFilePath] = useState("");
  const [fileFormat, setFileFormat] = useState("auto");
  const [previewedLocal, setPreviewedLocal] = useState(null);
  const [previewedImport, setPreviewedImport] = useState(null);
  const hasAgentRuns = Number(agentSummary.totalAgentRuns) > 0;
  const hasTraces = Number(agentSummary.totalTraces) > 0;
  const hasData = hasAgentRuns || hasTraces;
  const sourcePresentation = observedSourcePresentation(agentSummary);
  const [editing, setEditing] = useState(!hasData);
  const root = configUrl.replace(/\/api\/config$/, "");
  const serverOrigin = root || (typeof window === "undefined" ? "this Verdict server" : window.location.origin);
  useEffect(() => {
    fetch(configUrl.replace(/\/api\/config$/, "/api/setup/token"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((config) => setToken(config.setupToken))
      .catch((failure) => setError(setupFailureMessage(failure, serverOrigin)));
  }, [configUrl, serverOrigin]);
  useEffect(() => {
    if (hasData) setEditing(false);
  }, [hasData]);

  async function post(path, payload) {
    setBusy(true); setError(null);
    try {
      const response = await fetch(path, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      setResult(body); return body;
    } catch (failure) {
      setError(setupFailureMessage(failure, serverOrigin)); return null;
    } finally { setBusy(false); }
  }

  const sources = [
    ["local", "Claude Code / Codex"], ["file", "Existing telemetry file"],
    ["sdk", "Live app through SDK"], ["database", "Existing Verdict database"],
  ];
  const localKey = JSON.stringify([claudeRoot, codexRoot]);
  const importKey = JSON.stringify([filePath, fileFormat]);
  if (hasData && !editing) {
    const sourceText = (Array.isArray(agentSummary.agentRunSources) ? agentSummary.agentRunSources : [])
      .filter((item) => typeof item?.sourceKind === "string" && item.sourceKind && Number.isInteger(Number(item.runs)) && Number(item.runs) > 0)
      .map((item) => `${item.sourceKind}: ${item.runs}`)
      .join(" · ");
    return (
      <div className="max-w-4xl space-y-4">
        <section className={panel} style={style}>
          <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>OBSERVED DATA SOURCES</div>
          <h2 className="text-lg font-semibold mt-1">{sourcePresentation.heading}</h2>
          <div className="grid sm:grid-cols-3 gap-2 mt-4">
            <div className="border p-3" style={{ borderColor: "#26332e" }}><div className="text-xs" style={{ color: "#94a39d" }}>Agent Runs</div><div className="text-xl mt-1">{agentSummary.totalAgentRuns || 0}</div></div>
            <div className="border p-3" style={{ borderColor: "#26332e" }}><div className="text-xs" style={{ color: "#94a39d" }}>LLM Traces</div><div className="text-xl mt-1">{agentSummary.totalTraces || 0}</div></div>
            <div className="border p-3" style={{ borderColor: "#26332e" }}><div className="text-xs" style={{ color: "#94a39d" }}>{sourcePresentation.sourceLabel}</div><div className="text-sm mt-1">{sourceText || (hasAgentRuns ? "source details unavailable" : "trace records")}{agentSummary.agentRunSourcesTruncated ? " · additional sources omitted" : ""}</div></div>
          </div>
          {hasAgentRuns && <p className="text-sm mt-4" style={{ color: "#94a39d" }}>
            Newest observed run started: <span className="font-mono">{agentSummary.lastAgentCaptureAt || "unavailable"}</span>.
            {sourcePresentation.hasLocalAgents && <> Claude Code and Codex history can be manually rescanned or collected by a saved <code>verdict-service</code> schedule.</>}
            {sourcePresentation.hasSdkAgents && <> New instrumented activity appears when the SDK writes to this store or its capture files are imported.</>}
            {!sourcePresentation.hasLocalAgents && !sourcePresentation.hasSdkAgents && <> Add or import another source to collect more agent activity.</>}
          </p>}
          <button onClick={() => { if (sourcePresentation.hasSdkAgents && !sourcePresentation.hasLocalAgents) setSource("sdk"); setEditing(true); }} className="mt-4 border px-4 py-2 text-sm">Manage data sources</button>
        </section>
      </div>
    );
  }
  return (
    <div className="max-w-4xl space-y-4">
      <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>{hasData ? "ADD OR RESCAN DATA" : "1 · SOURCE"}</div>
        <h2 className="text-lg font-semibold mt-1">{hasData ? "Add, import, or rescan a source" : "What do you want to analyze?"}</h2>
        {hasData && <button onClick={() => setEditing(false)} className="mt-3 text-sm underline">Back to observed sources</button>}
        <div className="grid sm:grid-cols-2 gap-2 mt-4">
          {sources.map(([id, label]) => <button key={id} onClick={() => { setSource(id); setResult(null); }}
            className="border p-3 text-left text-sm" style={{ borderColor: source === id ? "#4ee1aa" : "#26332e", background: source === id ? "#18221e" : "transparent" }}>{label}</button>)}
        </div>
      </section>

      {source === "local" && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#56b6ff" }}>2 · PRIVACY AND PREVIEW</div>
        <label className="block text-sm mt-4">Claude history directory<input value={claudeRoot} onChange={(event) => { setClaudeRoot(event.target.value); setPreviewedLocal(null); }} className="block w-full mt-1 border p-2 bg-transparent" /></label>
        <label className="block text-sm mt-3">Codex history directory<input value={codexRoot} onChange={(event) => { setCodexRoot(event.target.value); setPreviewedLocal(null); }} className="block w-full mt-1 border p-2 bg-transparent" /></label>
        <p className="text-sm mt-4" style={{ color: "#94a39d" }}>Verdict retains bounded, recursively redacted request, response, tool, command, and test evidence for local agent analysis. Local setup uses content capture by default so the resulting run is actually evaluable.</p>
        <div className="flex gap-2 mt-4">
          <button disabled={!token || busy} onClick={async () => { const data = await post(`${root}/api/setup/preview`, { claudeRoot, codexRoot }); if (data) setPreviewedLocal(localKey); }} className="border px-4 py-2 text-sm">Preview sources</button>
          <button disabled={!token || busy || previewedLocal !== localKey} onClick={async () => { const data = await post(`${root}/api/setup/capture`, { claudeRoot, codexRoot, captureContent: true }); if (data) onComplete("local"); }} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Approve and capture</button>
        </div>
      </section>}

      {source === "file" && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#56b6ff" }}>2 · HISTORICAL IMPORT</div>
        <p className="text-sm mt-2" style={{ color: "#94a39d" }}>Uses Verdict's canonical bounded importer and preserves source event time.</p>
        <input value={filePath} onChange={(event) => { setFilePath(event.target.value); setPreviewedImport(null); }} placeholder="/path/to/export.jsonl or /path/to/export-directory" className="block w-full mt-4 border p-2 bg-transparent" />
        <select value={fileFormat} onChange={(event) => { setFileFormat(event.target.value); setPreviewedImport(null); }} className="mt-3 border p-2 bg-transparent">
          {["auto", "otlp", "langfuse", "langsmith", "datadog", "phoenix", "opik", "mlflow", "voice"].map((name) => <option key={name}>{name}</option>)}
        </select>
        <div className="flex gap-2 mt-4">
          <button disabled={!token || !filePath || busy} onClick={async () => { const data = await post(`${root}/api/setup/import/preview`, { path: filePath, format: fileFormat }); if (data) setPreviewedImport(importKey); }} className="border px-4 py-2 text-sm">Preview import</button>
          <button disabled={!token || !filePath || busy || previewedImport !== importKey} onClick={async () => { const data = await post(`${root}/api/setup/import`, { path: filePath, format: fileFormat }); if (data) onRefresh?.(); }} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Approve and import</button>
        </div>
      </section>}

      {source === "sdk" && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#56b6ff" }}>2 · LIVE SDK</div>
        <pre className="mt-4 p-4 overflow-x-auto text-sm" style={{ background: "#0b0e0d" }}>{`import verdict\nverdict.init(\n    storage="sqlite:///./verdict.db",\n    capture_content=True,\n    sample_rate=1.0,\n)\n\nwith verdict.agent_run(name="support-agent", session_id=session_id) as run:\n    with run.turn(user_input=user_message) as turn:\n        answer = handle_request(user_message)\n        turn.set_output(answer)`}</pre>
        <p className="text-sm mt-3" style={{ color: "#94a39d" }}>Supported provider calls inside the turn link to genuine LLM Traces automatically. Record tool, command, test, retry, feedback, and business-outcome evidence through the same turn. A few runs appear immediately; drift still needs reviewed reference and current cohorts.</p>
        <button onClick={() => onComplete("sdk")} className="mt-4 border px-4 py-2 text-sm">I ran a few calls — refresh Verdict</button>
      </section>}

      {source === "database" && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#56b6ff" }}>2 · EXISTING STORE</div>
        <p className="text-sm mt-3">Restart Verdict with <code>verdict --storage sqlite:///path/to/verdict.db</code> or a PostgreSQL DSN. The dashboard reads that store without copying its records.</p>
      </section>}

      {error && <div role="alert" className={panel} style={{ ...style, color: "#ff6b6b" }}>{error}</div>}
      {result && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>RESULT</div>
        <pre className="mt-3 text-xs overflow-x-auto">{JSON.stringify(result, null, 2)}</pre>
        {(result.summary?.stored > 0) && <div className="mt-4"><div className="text-xs font-mono" style={{ color: "#56b6ff" }}>3 · INITIAL ANALYSIS</div><p className="text-sm mt-2" style={{ color: "#94a39d" }}>Capture/import is complete. Review deterministic findings and evidence coverage first; a judge and clusters are optional next decisions.</p><div className="flex flex-wrap gap-2 mt-3"><button onClick={() => onNavigate?.("insights")} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Review findings</button><button onClick={() => onNavigate?.("evaluators")} className="border px-4 py-2 text-sm">Configure judge</button><button onClick={() => onNavigate?.("explore")} className="border px-4 py-2 text-sm">Explore cohorts / clusters</button><button onClick={() => onNavigate?.("control")} className="border px-4 py-2 text-sm">Schedule production monitoring</button></div></div>}
      </section>}
    </div>
  );
}
