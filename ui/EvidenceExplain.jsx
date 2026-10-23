import React, { useEffect, useState } from "react";

const C = { border: "#26332e", sub: "#94a39d", amber: "#f2b84b", red: "#ff6b6b", green: "#4ee1aa" };
const FOCUS = [
  ["failure", "What went wrong?"], ["tokens", "Why these tokens?"],
  ["latency", "What was slow?"], ["improvement", "What should I check next?"],
];
const UNAVAILABLE = {
  source_capture_partial: "Some events in this run were omitted by the capture limit. Inspect the recorded timeline; this selection is incomplete for AI analysis.",
  turn_content_partial: "The selected turn's request or response evidence is incomplete. Inspect its recorded events instead.",
  run_in_progress: "This run is still in progress. Inspect its recorded turns now or preview again after it ends.",
  evidence_limit: "This selection exceeds the bounded AI context. Inspect it locally or choose a smaller selection.",
  no_turns: "No agent turns were recorded for this run. Check capture before requesting an explanation.",
  call_not_eligible: "This call is unfinished or belongs to internal judge or replay work. Inspect its recorded facts instead.",
  diagnostic_redacted: "The selected failure details were masked. Inspect locally; AI cannot explain evidence it cannot see.",
  cluster_evidence_insufficient: "This cluster needs more assigned calls and representative prompts before AI can describe it.",
  run_limit: "This session contains too many runs for one AI context. Open a single run instead.",
  turn_limit: "This selection contains too many turns for one AI context. Open a single turn instead.",
  event_limit: "This selection contains too many events for one AI context. Open a single run or turn instead.",
  redaction_unavailable: "This evidence could not be safely redacted for AI. Inspect it locally instead.",
};
const unavailableMessage = (reason) => typeof reason === "string" && Object.hasOwn(UNAVAILABLE, reason)
  ? UNAVAILABLE[reason]
  : "The selected evidence is incomplete or unavailable. Inspect the recorded facts or choose a smaller selection.";

export function EvidenceExplain({ root, scope, runId = null, turnId = null, eventId = null,
  traceId = null, spanId = null, sessionId = null, sourceKind = null, onApplyDraft = null,
  versionId = null, clusterId = null, draftActionLabel = "Review draft in setup", draftKind = "monitor" }) {
  const [open, setOpen] = useState(false);
  const [auth, setAuth] = useState(null);
  const [environment, setEnvironment] = useState(null);
  const [provider, setProvider] = useState("anthropic");
  const [focus, setFocus] = useState(scope === "setup" ? "setup" : draftKind === "rubric" ? "rubric" : draftKind === "cluster_description" ? "cluster_description" : "failure");
  const [preview, setPreview] = useState(null);
  const [previewKey, setPreviewKey] = useState(null);
  const [approved, setApproved] = useState(false);
  const [budget, setBudget] = useState("0.05");
  const [result, setResult] = useState(null);
  const [resultKey, setResultKey] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((r) => r.json()),
      fetch(`${root}/api/evaluators`, { credentials: "same-origin" }).then((r) => r.json()),
    ]).then(([access, settings]) => { if (active) { setAuth(access.setupToken); setEnvironment(settings); } })
      .catch((failure) => { if (active) setError(String(failure)); });
    return () => { active = false; };
  }, [open, root]);
  const config = { scope, provider, focus, ...((scope === "setup" && draftKind === "cohort") || ["rubric", "cluster_description"].includes(draftKind) ? { draftKind } : {}),
    ...(runId ? { runId } : {}),
    ...(turnId ? { turnId } : {}), ...(eventId ? { eventId } : {}),
    ...(traceId ? { traceId } : {}),
    ...(spanId ? { spanId } : {}),
    ...(sessionId ? { sessionId, sourceKind } : {}),
    ...(versionId && clusterId ? { versionId, clusterId } : {}) };
  const key = JSON.stringify(config);
  const current = preview && previewKey === key;
  useEffect(() => {
    setApproved(false);
    setPreview(null);
    setResult(null);
  }, [key]);
  const configured = environment?.providers?.find((item) => item.provider === provider)?.configured;
  const cap = Number(budget);
  const withinBudget = Number.isFinite(cap) && cap > 0 && cap <= 100
    && preview?.upperStaticListPriceUsd != null && cap >= preview.upperStaticListPriceUsd;
  async function post(path, payload) {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": auth },
        body: JSON.stringify(payload) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(false); }
  }
  return <div className="mt-3 text-xs">
    <button type="button" className="border px-3 py-2" style={{ borderColor: C.green, color: C.green }} onClick={() => setOpen((value) => !value)}>{open ? "Hide AI assist" : draftKind === "cluster_description" ? "Ask AI to describe this cluster…" : draftKind === "rubric" ? "Ask AI for a rubric draft…" : scope === "setup" ? draftKind === "cohort" ? "Ask AI for a cohort draft…" : "Ask AI for a setup draft…" : `Explain this ${scope} with AI…`}</button>
    {open && <div className="border p-3 mt-2 space-y-3" style={{ borderColor: C.border }}>
      <p style={{ color: C.sub }}>{draftKind === "cluster_description" ? "Optional label and description from this cluster’s bounded representatives. Review them in the existing rename form; no cluster changes automatically." : draftKind === "rubric" ? "Optional criterion draft from the selected bounded evidence. Review and edit it in the evaluator form; no evaluation runs automatically." : scope === "setup" ? draftKind === "cohort" ? "Optional draft from aggregate eligible-call counts only. It fills comparison controls for your review; preview and activation remain separate decisions." : "Optional draft from source eligibility only. Review its suggested check in setup or comparison; running and activation remain separate decisions." : "Optional interpretation of the selected evidence. Local facts remain available without a key."} Nothing is sent until you review the exact context and approve one call.</p>
      <div className="grid sm:grid-cols-2 gap-2">{scope !== "setup" && !["rubric", "cluster_description"].includes(draftKind) && <label>Question<select value={focus} onChange={(event) => { setFocus(event.target.value); setApproved(false); }} className="block border p-2 mt-1 w-full bg-transparent">{FOCUS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}<label>Provider<select value={provider} onChange={(event) => { setProvider(event.target.value); setApproved(false); }} className="block border p-2 mt-1 w-full bg-transparent"><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option></select></label></div>
      <button type="button" className="border px-3 py-2" disabled={!auth || busy} onClick={async () => { const data = await post("/api/explain/preview", config); if (data) { setPreview(data); setPreviewKey(key); setApproved(false); setResult(null); } }}>Preview evidence and cost</button>
      {preview && !current && <p style={{ color: C.amber }}>Selection changed. Preview again.</p>}
      {current && <div className="border p-3 space-y-2" style={{ borderColor: C.border }}>
        {preview.plannedCalls !== 1 ? <p role="status" style={{ color: C.amber }}>AI explanation unavailable. {unavailableMessage(preview.evidencePreview?.reason)} No data was sent.</p> : <>
        <div>Bounded evidence available · {preview.destination}</div>
        <div style={{ color: C.sub }}>Static maximum list-price estimate: {preview.upperStaticListPriceUsd == null ? "unavailable" : `$${preview.upperStaticListPriceUsd.toFixed(4)}`}; actual billing may differ.</div>
        <details><summary className="cursor-pointer">Review exact redacted context and instructions</summary><pre className="whitespace-pre-wrap break-words max-h-72 overflow-auto mt-2">{JSON.stringify(preview.messages, null, 2)}</pre></details>
        <label className="block">Maximum approved static estimate (USD)<input type="number" min="0.001" max="100" step="0.001" value={budget} onChange={(event) => setBudget(event.target.value)} className="block border p-2 mt-1 w-32 bg-transparent" /></label>
        <label className="flex gap-2"><input type="checkbox" checked={approved} onChange={(event) => setApproved(event.target.checked)} disabled={!current || preview.plannedCalls !== 1 || !preview.providerAvailable} />I approve one call with this exact context to {preview.destination}.</label>
        <button type="button" className="border px-3 py-2" disabled={!approved || !withinBudget || !configured || !preview.providerAvailable || busy || preview.plannedCalls !== 1} onClick={async () => { const data = await post("/api/explain/run", { ...config, planFingerprint: preview.planFingerprint, maxEstimatedUsd: cap, confirmExternalEgress: true }); if (data) { setResult(data); setResultKey(key); } }}>{draftKind === "cluster_description" ? "Draft cluster description" : draftKind === "rubric" ? "Draft rubric criterion" : scope === "setup" ? draftKind === "cohort" ? "Draft cohort" : "Draft setup" : "Explain selected evidence"}</button>
        {!configured && <p style={{ color: C.amber }}>Provider key is not configured for this dashboard process.</p>}
        {!preview.providerAvailable && <p style={{ color: C.amber }}>This provider needs the Verdict eval package and its SDK installed in the dashboard environment.</p>}
        </>}
      </div>}
      {result && current && resultKey === key && <div role="status" className="border p-3 whitespace-pre-wrap" style={{ borderColor: C.border }}><div className="font-semibold">{draftKind === "cluster_description" ? "AI cluster draft" : draftKind === "rubric" ? "AI rubric draft" : scope === "setup" ? draftKind === "cohort" ? "AI cohort draft" : "AI setup draft" : "AI interpretation"} · {result.status}</div>{result.draft ? <><p className="mt-2">{result.draft.why}</p><p className="mt-2">{draftKind === "cluster_description" ? `${result.draft.displayName}: ${result.draft.description}` : draftKind === "rubric" ? `${result.draft.name}: ${result.draft.description}` : draftKind === "cohort" ? `Grouping: ${result.draft.grouping} · historical reference share: ${result.draft.referenceShare}% · minimum reference/current: ${result.draft.minimumReference}/${result.draft.minimumCurrent} · prospective target: ${result.draft.prospectiveTarget}` : `Check: ${result.draft.check} · grouping: ${result.draft.grouping} · suggested interval: ${result.draft.intervalHours} hours`}</p>{onApplyDraft && <button type="button" className="border px-3 py-2 mt-3" onClick={() => onApplyDraft(result.draft)}>{draftActionLabel}</button>}</> : result.answer || result.error || "No explanation available"}</div>}
      {error && <p role="alert" style={{ color: C.red }}>{error}</p>}
    </div>}
  </div>;
}
