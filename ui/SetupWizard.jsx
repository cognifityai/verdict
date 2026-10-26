import React, { useEffect, useRef, useState } from "react";
import {
  configuredStorePresentation,
  observedSourcePresentation,
  setupFailureMessage,
} from "./source-state.mjs";

const panel = "border p-5";
const style = { borderColor: "#26332e", background: "#111715" };
const LONG_CAPTURE = "Starting local history capture…";
const CAPTURE_POLL_MS = 1500;

function captureProgressLabel(job) {
  if (!job) return LONG_CAPTURE;
  if (job.state === "analyzing") return "Files imported. Analyzing captured evidence…";
  if (job.filesTotal == null) return "Listing history files…";
  return `Importing ${job.filesDone} of ${job.filesTotal} history files… Large histories can take several minutes. You can leave this page while Verdict keeps running.`;
}
const LONG_IMPORT = "Importing… Large files can take several minutes. Keep this page open.";

export function SetupWizard({ configUrl, onComplete, onNavigate, onRefresh, agentSummary = {} }) {
  const [token, setToken] = useState(null);
  const [source, setSource] = useState("local");
  const [busy, setBusy] = useState(false);
  const [busyLabel, setBusyLabel] = useState(null);
  const [result, setResult] = useState(null);
  const [latestLocalSummary, setLatestLocalSummary] = useState(null);
  const [attemptStatus, setAttemptStatus] = useState(null);
  const [activeJobId, setActiveJobId] = useState(null);
  const [cancelPending, setCancelPending] = useState(false);
  const captureGeneration = useRef(0);
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
  const storePresentation = configuredStorePresentation(agentSummary);
  const hasObservedStore = hasData || storePresentation.connectedEmpty;
  const sourcePresentation = observedSourcePresentation(agentSummary);
  const [editing, setEditing] = useState(!hasObservedStore);
  const root = configUrl.replace(/\/api\/config$/, "");
  const serverOrigin = root || (typeof window === "undefined" ? "this Verdict server" : window.location.origin);
  useEffect(() => {
    fetch(configUrl.replace(/\/api\/config$/, "/api/setup/token"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((config) => setToken(config.setupToken))
      .catch((failure) => setError(setupFailureMessage(failure, serverOrigin)));
  }, [configUrl, serverOrigin]);
  useEffect(() => {
    if (hasObservedStore) setEditing(false);
  }, [hasObservedStore]);

  async function post(path, payload, label) {
    setBusy(true); setBusyLabel(label); setError(null);
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
    } finally { setBusy(false); setBusyLabel(null); }
  }

  // Local capture runs on the server in the background: the POST returns the
  // job at once and this polls its status until the job ends.
  async function followCaptureJob(job) {
    setActiveJobId(job?.state === "running" ? job.jobId : null);
    while (job && (job.state === "running" || job.state === "analyzing")) {
      setBusyLabel(captureProgressLabel(job));
      await new Promise((resolve) => setTimeout(resolve, CAPTURE_POLL_MS));
      const status = await fetch(`${root}/api/setup/capture/status?job=${encodeURIComponent(job.jobId)}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (status.status === 404) throw new Error("another capture replaced this one; its result is shown under Data Sources");
      if (!status.ok) throw new Error(`HTTP ${status.status}`);
      const body = await status.json();
      if (body.attempts) setAttemptStatus(body.attempts);
      job = body.job;
      setActiveJobId(job?.state === "running" ? job.jobId : null);
    }
    setActiveJobId(null);
    if (job?.summary) setLatestLocalSummary(job.summary);
    if (!job || job.state !== "completed") {
      if (job?.state === "failed" || job?.state === "cancelled") onRefresh?.();
      throw new Error(job?.error || "capture did not complete");
    }
    const result = { summary: job.summary, analysis: job.analysis };
    setLatestLocalSummary(job.summary);
    setResult(result); return result;
  }

  async function cancelCapture() {
    if (!activeJobId || !token || cancelPending) return;
    setCancelPending(true);
    try {
      const response = await fetch(`${root}/api/setup/capture/${encodeURIComponent(activeJobId)}/cancel`, {
        method: "POST", credentials: "same-origin", headers: { "X-Verdict-Setup": token },
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      setBusyLabel("Stopping capture after the current file…");
    } catch (failure) {
      setError(setupFailureMessage(failure, serverOrigin));
      setCancelPending(false);
    }
  }

  async function capture(payload) {
    setBusy(true); setBusyLabel(LONG_CAPTURE); setError(null);
    try {
      const response = await fetch(`${root}/api/setup/capture`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      captureGeneration.current += 1;
      return await followCaptureJob(body.job);
    } catch (failure) {
      setError(setupFailureMessage(failure, serverOrigin)); return null;
    } finally { setBusy(false); setBusyLabel(null); setActiveJobId(null); setCancelPending(false); }
  }

  // A capture keeps running on the server if this page is closed or reloaded.
  // On load, pick up the job that is still running and show its progress.
  useEffect(() => {
    let active = true;
    const generation = captureGeneration.current;
    fetch(`${root}/api/setup/capture/status`, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : null)
      .then(async (body) => {
        const job = body?.job;
        if (!active || generation !== captureGeneration.current) return;
        if (body?.attempts) setAttemptStatus(body.attempts);
        if (!job) return;
        if (body.lastImportedSummary) setLatestLocalSummary(body.lastImportedSummary);
        if (job.summary) setLatestLocalSummary(job.summary);
        if (job.state === "completed") {
          return;
        }
        if (job.state !== "running" && job.state !== "analyzing") return;
        setBusy(true); setError(null);
        try { if (await followCaptureJob(job)) onComplete("local"); }
        catch (failure) { if (active) setError(setupFailureMessage(failure, serverOrigin)); }
        finally { if (active) { setBusy(false); setBusyLabel(null); setActiveJobId(null); setCancelPending(false); } }
      })
      .catch(() => {});
    return () => { active = false; };
  }, [root]);  // eslint-disable-line react-hooks/exhaustive-deps

  const sources = [
    ["local", "Claude Code / Codex"], ["file", "Existing telemetry file"],
    ["sdk", "Live app through SDK"], ["database", "Existing Verdict database"],
  ];
  const localKey = JSON.stringify([claudeRoot, codexRoot]);
  const importKey = JSON.stringify([filePath, fileFormat]);
  const statusNotice = busy && busyLabel && <div role="status" aria-live="polite" className={panel} style={{ ...style, color: "#94a39d" }}>{busyLabel}{activeJobId && <button type="button" disabled={cancelPending} onClick={cancelCapture} className="ml-4 underline">Cancel capture</button>}</div>;
  const errorNotice = error && <div role="alert" className={panel} style={{ ...style, color: "#ff6b6b" }}>{error}</div>;
  const limitedSessionCount = Number.isInteger(latestLocalSummary?.event_limited_runs) && latestLocalSummary.event_limited_runs > 0
    ? latestLocalSummary.event_limited_runs : 0;
  const recentSampleErrors = Number.isInteger(latestLocalSummary?.recent_sample_errors) && latestLocalSummary.recent_sample_errors > 0
    ? latestLocalSummary.recent_sample_errors : 0;
  const skippedFiles = Number.isInteger(latestLocalSummary?.skipped) && latestLocalSummary.skipped > 0
    ? latestLocalSummary.skipped : 0;
  const recentPartialFiles = Number.isInteger(latestLocalSummary?.source_partial_files) && latestLocalSummary.source_partial_files > 0
    ? latestLocalSummary.source_partial_files : 0;
  const durablePartialFiles = Math.max(0, Number(attemptStatus?.sourcePartialFiles) || 0);
  const sourcePartialFiles = Math.max(recentPartialFiles, durablePartialFiles);
  const partialReasons = durablePartialFiles >= recentPartialFiles && durablePartialFiles > 0
    ? attemptStatus?.partialReasons || {} : latestLocalSummary?.partial_reasons || {};
  const partialReasonText = [
    partialReasons.bounded_source_prefix > 0 && "bounded source prefix",
    partialReasons.unfinished_final_record > 0 && "unfinished final record",
  ].filter(Boolean).join(", ");
  const sourcePartialNotice = sourcePartialFiles > 0 && <div role="alert" className={panel} style={{ ...style, color: "#f2c879" }}>
    {sourcePartialFiles} {sourcePartialFiles === 1 ? "history file" : "history files"} had partial source coverage ({partialReasonText || "source tail unavailable"}). Captured evidence is available, but the unobserved tail cannot support a complete-session conclusion.
  </div>;
  const incompleteNotice = skippedFiles > 0 && <div role="alert" className={panel} style={{ ...style, color: "#f2c879" }}>
    Local history capture is incomplete: {skippedFiles} skipped. Captured evidence is available. Check the skipped file reasons, then rescan after resolving them.
  </div>;
  const durableAttemptNotice = attemptStatus?.failedFiles > 0 && <div role="alert" className={panel} style={{ ...style, color: "#f2c879" }}>
    Latest recorded scan attempts failed for {attemptStatus.failedFiles} {attemptStatus.failedFiles === 1 ? "history file" : "history files"}. Reasons: {Object.entries(attemptStatus.failureReasons || {}).map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(", ") || "unavailable"}. Check the current source files and rescan. This status survives a dashboard restart.
  </div>;
  const unfinishedFiles = Math.max(0, Number(attemptStatus?.unfinishedFiles) || 0);
  const cancelledFiles = Math.max(0, Number(attemptStatus?.cancelledFiles) || 0);
  const interruptedAttemptNotice = (unfinishedFiles > 0 || cancelledFiles > 0) && <div role="alert" className={panel} style={{ ...style, color: "#f2c879" }}>
    Latest recorded scan attempts include {unfinishedFiles} interrupted and {cancelledFiles} cancelled history files. Captured evidence remains available, but those files may be incomplete. Rescan to finish capturing them.
  </div>;
  const coverageNotice = limitedSessionCount > 0 && <div role="alert" className={panel} style={{ ...style, color: "#f2c879" }}>
    A recent local import reported {limitedSessionCount} {limitedSessionCount === 1 ? "session" : "sessions"} beyond the bounded run view. Open the run to inspect its recent sample and use Browse recorded events when a complete captured page is available. Selected recorded failures can become monitored issues after monitoring is activated, and linked model calls can support call-level evaluation. The bounded run view alone cannot establish a whole-run issue or no-issue result.
  </div>;
  const sampleErrorNotice = recentSampleErrors > 0 && <div role="alert" className={panel} style={{ ...style, color: "#f2c879" }}>
    A recent local import could not refresh the recent event sample for {recentSampleErrors} {recentSampleErrors === 1 ? "session" : "sessions"}. An older dated sample may still be visible. Check the source file and rescan after resolving the conflict.
  </div>;
  if (hasObservedStore && !editing) {
    const sourceText = (Array.isArray(agentSummary.agentRunSources) ? agentSummary.agentRunSources : [])
      .filter((item) => typeof item?.sourceKind === "string" && item.sourceKind && Number.isInteger(Number(item.runs)) && Number(item.runs) > 0)
      .map((item) => `${item.sourceKind}: ${item.runs}`)
      .join(" · ");
    return (
      <div className="max-w-4xl space-y-4">
        {statusNotice}
        {errorNotice}
        {incompleteNotice}
        {sourcePartialNotice}
        {durableAttemptNotice}
        {interruptedAttemptNotice}
        {coverageNotice}
        {sampleErrorNotice}
        <section className={panel} style={style}>
          <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>{storePresentation.connectedEmpty ? "CONNECTED VERDICT STORE" : "OBSERVED DATA SOURCES"}</div>
          <h2 className="text-lg font-semibold mt-1">{storePresentation.connectedEmpty ? `Connected ${storePresentation.backendLabel} store` : sourcePresentation.heading}</h2>
          <div className="grid sm:grid-cols-3 gap-2 mt-4">
            <div className="border p-3" style={{ borderColor: "#26332e" }}><div className="text-xs" style={{ color: "#94a39d" }}>Agent Runs</div><div className="text-xl mt-1">{agentSummary.totalAgentRuns || 0}</div></div>
            <div className="border p-3" style={{ borderColor: "#26332e" }}><div className="text-xs" style={{ color: "#94a39d" }}>LLM Traces</div><div className="text-xl mt-1">{agentSummary.totalTraces || 0}</div></div>
            <div className="border p-3" style={{ borderColor: "#26332e" }}><div className="text-xs" style={{ color: "#94a39d" }}>{storePresentation.connectedEmpty ? "Storage backend" : sourcePresentation.sourceLabel}</div><div className="text-sm mt-1">{storePresentation.connectedEmpty ? storePresentation.backendLabel : (sourceText || (hasAgentRuns ? "source details unavailable" : "trace records"))}{agentSummary.agentRunSourcesTruncated ? " · additional sources omitted" : ""}</div></div>
          </div>
          {storePresentation.connectedEmpty && <p className="text-sm mt-4" style={{ color: "#94a39d" }}>Verdict is connected and waiting for live SDK traffic or an approved import. No Agent Runs or LLM Traces are visible for this tenant yet.</p>}
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
      {durableAttemptNotice}
      {interruptedAttemptNotice}
      <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>{hasObservedStore ? "ADD OR RESCAN DATA" : "1 · SOURCE"}</div>
        <h2 className="text-lg font-semibold mt-1">{hasObservedStore ? "Add, import, or rescan a source" : "What do you want to analyze?"}</h2>
        {hasObservedStore && <button onClick={() => setEditing(false)} className="mt-3 text-sm underline">Back to connected store</button>}
        <div className="grid sm:grid-cols-2 gap-2 mt-4">
          {sources.map(([id, label]) => <button key={id} onClick={() => { setSource(id); setResult(null); }}
            className="border p-3 text-left text-sm" style={{ borderColor: source === id ? "#4ee1aa" : "#26332e", background: source === id ? "#18221e" : "transparent" }}>{label}</button>)}
        </div>
      </section>

      {source === "local" && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#56b6ff" }}>2 · PRIVACY AND PREVIEW</div>
        <label className="block text-sm mt-4">Claude history directory<input value={claudeRoot} onChange={(event) => { setClaudeRoot(event.target.value); setPreviewedLocal(null); }} className="block w-full mt-1 border p-2 bg-transparent" /></label>
        <label className="block text-sm mt-3">Codex history directory<input value={codexRoot} onChange={(event) => { setCodexRoot(event.target.value); setPreviewedLocal(null); }} className="block w-full mt-1 border p-2 bg-transparent" /></label>
        <p className="text-sm mt-4" style={{ color: "#94a39d" }}>Verdict retains bounded, recursively redacted request, response, tool, command, and test evidence for local agent analysis. For the standard Codex directory, it also reads completed model-call metadata from the sibling <code>logs_2.sqlite</code> database; diagnostic bodies are never stored. Local setup uses content capture by default so the resulting run is actually evaluable.</p>
        <div className="flex gap-2 mt-4">
          <button disabled={!token || busy} onClick={async () => { const data = await post(`${root}/api/setup/preview`, { claudeRoot, codexRoot }, "Previewing sources…"); if (data) setPreviewedLocal(localKey); }} className="border px-4 py-2 text-sm">Preview sources</button>
          <button disabled={!token || busy || previewedLocal !== localKey} onClick={async () => { const data = await capture({ claudeRoot, codexRoot, captureContent: true }); if (data) onComplete("local"); }} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Approve and capture</button>
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
          <button disabled={!token || !filePath || busy} onClick={async () => { const data = await post(`${root}/api/setup/import/preview`, { path: filePath, format: fileFormat }, "Previewing import…"); if (data) setPreviewedImport(importKey); }} className="border px-4 py-2 text-sm">Preview import</button>
          <button disabled={!token || !filePath || busy || previewedImport !== importKey} onClick={async () => { const data = await post(`${root}/api/setup/import`, { path: filePath, format: fileFormat }, LONG_IMPORT); if (data) onRefresh?.(); }} className="px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Approve and import</button>
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

      {statusNotice}
      {errorNotice}
      {incompleteNotice}
      {sourcePartialNotice}
      {coverageNotice}
      {sampleErrorNotice}
      {result && <section className={panel} style={style}>
        <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>RESULT</div>
        <pre className="mt-3 text-xs overflow-x-auto">{JSON.stringify(result, null, 2)}</pre>
        {(result.summary?.stored > 0) && <div className="mt-4"><div className="text-xs font-mono" style={{ color: "#56b6ff" }}>3 · REVIEW THE EVIDENCE</div><p className="text-sm mt-2" style={{ color: "#94a39d" }}>Import finished. Review what was captured and any recorded failures, then follow the suggested monitoring plan. Answer-quality evaluation remains a separate choice.</p><button onClick={() => onNavigate?.("insights")} className="mt-3 px-4 py-2 text-sm" style={{ background: "#4ee1aa", color: "#0b0e0d" }}>Open overview and next step</button></div>}
      </section>}
    </div>
  );
}
