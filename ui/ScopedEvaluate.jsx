import React, { useEffect, useState } from "react";

const C = { border: "#26332e", sub: "#94a39d", amber: "#f2b84b", red: "#ff6b6b", green: "#4ee1aa" };

export function ScopedEvaluate({ root, scope, runId = null, eventId = null, sessionId = null, sourceKind = null }) {
  const previewSequence = React.useRef(0);
  const [open, setOpen] = useState(false);
  const [auth, setAuth] = useState(null);
  const [environment, setEnvironment] = useState(null);
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("claude-haiku-4-5");
  const [criterion, setCriterion] = useState(scope === "tool_action"
    ? "Given the recorded task and available tool contract, did the selected tool and arguments fit? Successful execution alone is insufficient."
    : "Did the ordered recorded work satisfy the stated task? Allow multiple valid approaches and distinguish missing evidence from failure.");
  const [preview, setPreview] = useState(null);
  const [previewKey, setPreviewKey] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [scheduleConfirmed, setScheduleConfirmed] = useState(false);
  const [scheduleBudget, setScheduleBudget] = useState("0.05");
  const [scheduledJob, setScheduledJob] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((r) => r.json()),
      fetch(`${root}/api/evaluators`, { credentials: "same-origin" }).then((r) => r.json()),
    ]).then(([access, env]) => { if (active) { setAuth(access.setupToken); setEnvironment(env); } })
      .catch((failure) => { if (active) setError(String(failure)); });
    return () => { active = false; };
  }, [open, root]);
  const config = { scope, ...(runId ? { runId } : {}), ...(eventId ? { eventId } : {}),
    ...(sessionId ? { sessionId, sourceKind } : {}), provider, model,
    maxOutputTokens: 256,
    rubric: { name: scope === "tool_action" ? "tool_review" : scope === "session" ? "session_review" : "run_review", version: "1",
      dimensions: [{ name: scope === "tool_action" ? "tool_fit" : "task_completion", description: criterion }] },
  };
  const key = JSON.stringify(config);
  const current = preview && previewKey === key;
  const configured = environment?.providers?.find((item) => item.provider === provider)?.configured;
  const post = async (path, body) => {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": auth }, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(false); }
  };
  return <div className="mt-3 text-xs">
    <button type="button" className="border px-3 py-2" onClick={() => setOpen((value) => !value)}>{open ? "Hide evaluation" : scope === "run" ? "Evaluate this run…" : scope === "session" ? "Evaluate this session…" : "Evaluate tool action…"}</button>
    {open && <div className="border p-3 mt-2 space-y-3" style={{ borderColor: C.border }}>
      <p style={{ color: C.sub }}>Optional external judge. Analyze and recorded findings remain available without a provider key. No evidence is sent until you review and approve the preview.</p>
      <div className="grid sm:grid-cols-2 gap-2"><label>Provider<select className="block border p-2 mt-1 w-full bg-transparent" value={provider} onChange={(event) => { setProvider(event.target.value); setConfirmed(false); }}><option value="anthropic">Anthropic</option><option value="openai">OpenAI / compatible</option><option value="google">Google</option></select></label><label>Judge model<input className="block border p-2 mt-1 w-full bg-transparent" value={model} onChange={(event) => { setModel(event.target.value); setConfirmed(false); }} /></label></div>
      <label>Check<textarea className="block border p-2 mt-1 w-full bg-transparent" value={criterion} onChange={(event) => { setCriterion(event.target.value); setConfirmed(false); }} /></label>
      <button type="button" className="border px-3 py-2" disabled={!auth || busy} onClick={async () => { const sequence = ++previewSequence.current; const data = await post("/api/scoped/preview", config); if (data && sequence === previewSequence.current) { setPreview(data); setPreviewKey(key); setConfirmed(false); setScheduleConfirmed(false); setScheduledJob(null); setResult(null); fetch(`${root}/api/scoped/scheduled`, { credentials: "same-origin" }).then((response) => response.ok ? response.json() : null).then((jobs) => { if (sequence === previewSequence.current) setScheduledJob(jobs?.jobs?.find((item) => item.planFingerprint === data.planFingerprint) || null); }).catch(() => {}); } }}>Preview bounded evidence</button>
      {preview && <div className="border p-3" style={{ borderColor: C.border }}>
        <p>{preview.coverage === "complete" ? "Complete bounded scope" : `Partial / unavailable: ${preview.unavailableReason?.replaceAll("_", " ") || "evidence unavailable"}`} · {preview.plannedCalls} planned call{preview.plannedCalls === 1 ? "" : "s"} · {preview.provider}/{preview.model}</p>
        <p style={{ color: C.sub }}>Destination: {preview.destination}. Estimated maximum list price: {preview.estimatedMaximumCostUsd == null ? "unavailable" : `$${preview.estimatedMaximumCostUsd.toFixed(4)}`}; actual billing may differ.</p>
        {preview.plannedCalls > 0 && <details className="mt-2"><summary>Review exact redacted evidence</summary><pre className="whitespace-pre-wrap break-words max-h-72 overflow-auto mt-2">{JSON.stringify(preview.evidencePreview, null, 2)}</pre></details>}
        {!current && <p style={{ color: C.amber }}>The selection or check changed. Preview again.</p>}
        <label className="flex gap-2 mt-3"><input type="checkbox" disabled={!current || preview.plannedCalls !== 1} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I approve sending this exact bounded evidence and check to {preview.destination} for one judge call.</label>
        <button type="button" className="border px-3 py-2 mt-2" disabled={!current || !confirmed || !configured || busy || preview.plannedCalls !== 1} onClick={async () => { const data = await post("/api/scoped/run", { ...config, planFingerprint: preview.planFingerprint, confirmExternalEgress: true }); if (data) setResult(data); }}>Run evaluation</button>
        {!configured && <p style={{ color: C.amber }}>Provider key is not configured for this dashboard process.</p>}
        {current && preview.plannedCalls === 1 && ["anthropic", "openai"].includes(provider) && <div className="border-t pt-3 mt-3 space-y-2" style={{ borderColor: C.border }}>
          <div className="font-semibold">Queue one evaluation for the next local service cycle</div>
          <p style={{ color: C.sub }}>The exact evidence and check above are authorized once. If evidence changes before the worker runs, the approval is cancelled. An interrupted or timed-out attempt is never retried automatically.</p>
          <p>Conservative static list-price bound: {preview.scheduledUpperStaticListPriceUsd == null ? "unavailable" : `$${preview.scheduledUpperStaticListPriceUsd.toFixed(4)}`}. Actual provider billing may differ.</p>
          <label>Maximum approved static estimate (USD)<input className="block border p-2 mt-1 w-32 bg-transparent" type="number" min="0.001" max="100" step="0.001" value={scheduleBudget} onChange={(event) => setScheduleBudget(event.target.value)} /></label>
          <label className="flex gap-2"><input type="checkbox" checked={scheduleConfirmed} onChange={(event) => setScheduleConfirmed(event.target.checked)} />I authorize this exact evidence and check to be sent to {preview.destination} once during a later local service cycle, within the static estimate limit above.</label>
          {!scheduledJob && <button type="button" className="border px-3 py-2" disabled={!scheduleConfirmed || busy || !Number.isFinite(Number(scheduleBudget)) || Number(scheduleBudget) <= 0 || Number(scheduleBudget) > 100 || Number(scheduleBudget) < (preview.scheduledUpperStaticListPriceUsd ?? Infinity)} onClick={async () => { const data = await post("/api/scoped/schedule", { ...config, planFingerprint: preview.planFingerprint, maxEstimatedUsd: Number(scheduleBudget), confirmPersistentExternalEgress: true }); if (data) setScheduledJob(data); }}>Queue one evaluation</button>}
          {scheduledJob && <div role="status">Scheduled approval: {scheduledJob.state}{scheduledJob.outcome ? ` · ${scheduledJob.outcome.replaceAll("_", " ")}` : ""}{scheduledJob.resultId ? ` · saved result ${scheduledJob.resultId.slice(0, 12)}` : ""}{scheduledJob.error ? ` · ${scheduledJob.error}` : ""}. {scheduledJob.resultId && <button type="button" className="underline ml-2" onClick={async () => { try { const response = await fetch(`${root}/api/scoped/result/${encodeURIComponent(scheduledJob.resultId)}`, { credentials: "same-origin" }); if (!response.ok) throw new Error(`HTTP ${response.status}`); setResult(await response.json()); setError(null); } catch (failure) { setError(String(failure)); } }}>Open saved result</button>}{scheduledJob.state === "pending" && <button type="button" className="underline ml-2" disabled={busy} onClick={async () => { const data = await post(`/api/scoped/scheduled/${scheduledJob.jobId}/cancel`, { expectedRevision: scheduledJob.revision }); if (data) setScheduledJob({ ...scheduledJob, ...data }); }}>Cancel approval</button>}</div>}
        </div>}
      </div>}
      {result && <div className="border p-3" style={{ borderColor: C.border }}><p>Evaluation {result.status} · evidence {result.coverage} · evaluator {result.evaluatorFingerprint}</p>{result.dimensions?.map((item) => <p key={item.name} className="mt-2">{item.name}: {item.verdict}{item.citationStatus !== "linked" ? " · Uncited judge opinion" : ""} · {item.explanation} {item.evidenceReferences?.join(", ")}</p>)}{result.error && <p style={{ color: C.red }}>Judge result unavailable: {result.error}</p>}</div>}
      {error && <p role="alert" style={{ color: C.red }}>{error}</p>}
    </div>}
  </div>;
}
