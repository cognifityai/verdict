import React, { useEffect, useState } from "react";

const C = { border: "#26332e", sub: "#94a39d", amber: "#f2b84b", red: "#ff6b6b", green: "#4ee1aa" };
const DEFAULT_MODELS = { anthropic: "claude-haiku-4-5", openai: "gpt-4o-mini", google: "gemini-3.5-flash-lite" };
const REVIEW_CHECKS = {
  task_completion: { label: "Task completion", description: "PASS when the recorded work supports completion of the stated task; FAIL when recorded evidence shows a material unmet requirement or contradiction. Allow multiple valid approaches. Return UNCLEAR when completion cannot be checked from this capture." },
  correction: { label: "Correction or re-explanation", description: "PASS when the prior answer addresses the recorded request and the next request is a new task or ordinary refinement. FAIL when the user had to correct or re-explain that earlier request because the agent missed it. Cite both turns; return UNCLEAR when intent or cause is not supported by the captured evidence." },
  unsupported_completion: { label: "Unsupported completion claim", description: "PASS when a recorded completion claim is supported by the recorded evidence. FAIL when the agent claimed the requested work was done and recorded evidence contradicts that claim. Cite the claim and evidence; return UNCLEAR when no claim is recorded or completion cannot be checked." },
  status_followup: { label: "Status follow-up", description: "PASS when the recorded pair supports that the agent kept the user informed during a long task without a status chase. FAIL when the user explicitly says they had to chase status and recorded agent behavior supports that complaint. Return UNCLEAR when delivery of progress, timing, or the reason for the follow-up cannot be established; absent progress records alone are not proof of silence." },
  requested_brevity: { label: "Requested brevity", description: "PASS when the response respects an explicit recorded request for brevity. FAIL when it materially ignores that request. Cite the request and response; return UNCLEAR when the request or response is missing or brevity was not requested." },
  factual_history: { label: "Material factual accuracy", description: "PASS when no material factual claim in the assessed response is false under the declared evidence authority. FAIL when authoritative evidence demonstrates a material factual error. A user allegation alone is insufficient. Return UNCLEAR when available evidence cannot establish factual accuracy." },
};
const UNAVAILABLE = {
  source_capture_partial: "Events after the capture limit may be missing from this run. Inspect the two recorded Turns locally; this pair cannot support a complete behavior judgment.",
  evidence_limit: "This pair exceeds the bounded review context. Inspect the recorded Turns locally; no judge request can be made for this selection.",
  turn_content_partial: "A request or answer in this pair is incomplete. If the prior answer and both requests are recorded, a separate text-only check may still be available.",
  required_evidence_unavailable: "Required substantive evidence is explicitly missing or recorded only as a summary, count, or reference. Structural coverage does not establish evaluation suitability.",
  audit_shape_limit: "A historical audit supports at most four turn-by-dimension entries, four turns, and one holistic check, with at least 2,048 output tokens. Choose a smaller selection.",
  result_limit: "This selection's result metadata exceeds the bounded storage allowance. Choose a smaller selection or rubric before requesting a judge call.",
};

export function EvidenceSuitability({ suitability }) {
  if (!suitability) return null;
  const missing = [...new Set((suitability.missingRequiredEvidence || []).map((item) => `${item.field}: ${item.role}`))];
  return <p className="text-xs mt-2 break-words" style={{ color: C.sub }}>Evaluation suitability: {suitability.state || "unknown"}.
    {missing.length > 0 ? ` Required substantive evidence unavailable (${missing.join("; ")}).` : ""}
    {suitability.limitations?.length > 0 ? ` ${suitability.limitations.join(" ")}` : ""}</p>;
}

// auditOnly: ordinary evaluation is unavailable but a historical audit, which
// discloses partial event capture, may still run; preview enforces the rest.
export function ScopedEvaluate({ root, scope, runId = null, turnId = null, eventId = null, sessionId = null, sourceKind = null, auditOnly = false }) {
  const previewSequence = React.useRef(0);
  const [open, setOpen] = useState(false);
  const [auth, setAuth] = useState(null);
  const [environment, setEnvironment] = useState(null);
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState(DEFAULT_MODELS.anthropic);
  const [reviewFocus, setReviewFocus] = useState(scope === "turn_window" ? "correction" : "task_completion");
  const [textOnly, setTextOnly] = useState(false);
  const [auditMode, setAuditMode] = useState(auditOnly);
  const [aggregation, setAggregation] = useState("historical_all_turns");
  const [authority, setAuthority] = useState("recorded_sources");
  const [requireSources, setRequireSources] = useState(false);
  const [criterion, setCriterion] = useState(scope === "tool_action"
    ? "Given the recorded task and available tool contract, did the selected tool and arguments fit? Successful execution alone is insufficient."
    : REVIEW_CHECKS[scope === "turn_window" ? "correction" : "task_completion"].description);
  const [preview, setPreview] = useState(null);
  const [previewKey, setPreviewKey] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [scheduleConfirmed, setScheduleConfirmed] = useState(false);
  const [estimateLimit, setEstimateLimit] = useState("0.05");
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
  const effectiveScope = scope === "turn_window" && textOnly ? "followup_text" : scope;
  const config = { scope: effectiveScope, ...(runId ? { runId } : {}), ...(turnId ? { turnId } : {}), ...(eventId ? { eventId } : {}),
    ...(sessionId ? { sessionId, sourceKind } : {}), provider, model,
    evaluationMode: auditMode && !textOnly ? "historical_audit" : "original_response",
    maxOutputTokens: auditMode && !textOnly ? 4096 : 256,
    ...(textOnly && scope === "turn_window" ? {} : { rubric: { name: scope === "tool_action" ? "tool_review" : scope === "session" ? "session_review" : scope === "turn_window" ? "adjacent_turn_review" : "run_review", version: "1",
      dimensions: [{ name: scope === "tool_action" ? "tool_fit" : reviewFocus === "custom" ? "custom_review" : reviewFocus, description: criterion,
        ...(auditMode ? { aggregation, evidenceAuthority: authority, requiredEvidence: requireSources ? ["request", "response", "sources"] : ["request", "response"] } : {}) }] },
    }),
  };
  const key = JSON.stringify(config);
  const current = preview && previewKey === key;
  const approvedEstimate = Number(estimateLimit);
  const withinEstimateLimit = Number.isFinite(approvedEstimate) && approvedEstimate > 0
    && approvedEstimate <= 100 && approvedEstimate >= (preview?.upperStaticListPriceUsd ?? Infinity);
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
    <button type="button" className="border px-3 py-2" onClick={() => setOpen((value) => !value)}>{open ? "Hide evaluation" : scope === "run" ? "Evaluate this run…" : scope === "session" ? "Evaluate this session…" : scope === "turn_window" ? "Review this turn and prior answer…" : "Evaluate tool action…"}</button>
    {open && <div className="border p-3 mt-2 space-y-3" style={{ borderColor: C.border }}>
      <p style={{ color: C.sub }}>Optional external judge. Analyze and recorded findings remain available without a provider key. No evidence is sent until you review and approve the preview.</p>
      {["session", "run", "turn_window"].includes(scope) && !textOnly && <>
        <label className="flex gap-2"><input type="checkbox" aria-label="Historical audit" checked={auditMode} disabled={auditOnly} onChange={(event) => { setAuditMode(event.target.checked); setConfirmed(false); }} />Historical audit: assess each original response separately and track later recovery per finding.</label>
        {auditOnly && <p style={{ color: C.sub }}>Only a historical audit is available for this selection: its event capture is partial or its order is uncertain, and the audit reports that limitation.</p>}
        {auditMode && <div className="space-y-2">
          <label>Aggregation<select aria-label="Audit aggregation" className="block border p-2 mt-1 w-full bg-transparent" value={aggregation} onChange={(event) => { setAggregation(event.target.value); setConfirmed(false); }}><option value="historical_all_turns">Historical: every assessed response must pass; recovery preserves earlier failures</option><option value="final_response">Final selected response: completion at the end</option><option value="holistic">Holistic: assess the selected work as a whole</option></select></label>
          <label>Evidence authority<select aria-label="Evidence authority" className="block border p-2 mt-1 w-full bg-transparent" value={authority} onChange={(event) => { setAuthority(event.target.value); setConfirmed(false); }}><option value="recorded_sources">Recorded source evidence</option><option value="public_knowledge">Recorded sources and well-established public knowledge</option></select></label>
          <label className="flex gap-2"><input type="checkbox" checked={requireSources} onChange={(event) => { setRequireSources(event.target.checked); setConfirmed(false); }} />Require substantive source capture in addition to requests and responses</label>
          <p style={{ color: C.sub }}>One call, up to four turn-by-dimension entries. Requested output allowance: 4,096 tokens; any provider reasoning allowance is included in preview. This changes the evaluator identity. Unknown legacy provenance remains unknown.</p>
        </div>}
      </>}
      {scope === "turn_window" && <p style={{ color: C.sub }}>{textOnly ? "Only the two recorded Turns and the prior answer are reviewed. Typed events and later agent work are excluded; this cannot establish agent fault or task success." : "Only these two recorded turns are reviewed. This does not assess the surrounding run or prove the agent was silent."}</p>}
      <div className="grid sm:grid-cols-2 gap-2"><label>Provider<select className="block border p-2 mt-1 w-full bg-transparent" value={provider} onChange={(event) => { const next = event.target.value; setProvider(next); setModel(DEFAULT_MODELS[next]); setConfirmed(false); }}><option value="anthropic">Anthropic</option><option value="openai">OpenAI / compatible</option><option value="google">Google</option></select></label><label>Judge model<input className="block border p-2 mt-1 w-full bg-transparent" value={model} onChange={(event) => { setModel(event.target.value); setConfirmed(false); }} /></label></div>
      {scope !== "tool_action" && !textOnly && <label>Review focus<select aria-label="Review focus" className="block border p-2 mt-1 w-full bg-transparent" value={reviewFocus} onChange={(event) => { const next = event.target.value; setReviewFocus(next); if (REVIEW_CHECKS[next]) setCriterion(REVIEW_CHECKS[next].description); setConfirmed(false); }}>
        {Object.entries(REVIEW_CHECKS).map(([value, check]) => <option key={value} value={value}>{check.label}</option>)}
        {reviewFocus === "custom" && <option value="custom">Custom check</option>}
      </select></label>}
      {textOnly ? <p>Fixed check: Did the later request explicitly correct or re-explain the preceding exchange? The criterion asks for UNCLEAR when the evidence cannot decide; the judge may still misclassify the pair. Review its reasoning before treating a result as a correction.</p> : <label>Check<textarea className="block border p-2 mt-1 w-full bg-transparent" value={criterion} onChange={(event) => { setCriterion(event.target.value); if (scope !== "tool_action") setReviewFocus("custom"); setConfirmed(false); }} /></label>}
      {environment?.evalPackageAvailable === false && <p role="status" style={{ color: C.amber }}>Install the optional cognifity-verdict-eval package in this dashboard's Python environment to use judge reviews. Local analysis remains available.</p>}
      <button type="button" className="border px-3 py-2" disabled={!auth || busy || environment?.evalPackageAvailable === false} onClick={async () => { const sequence = ++previewSequence.current; const data = await post("/api/scoped/preview", config); if (data && sequence === previewSequence.current) { setPreview(data); setPreviewKey(key); setConfirmed(false); setScheduleConfirmed(false); setScheduledJob(null); setResult(data.savedResult || null); if (data.plannedCalls === 1) fetch(`${root}/api/scoped/scheduled`, { credentials: "same-origin" }).then((response) => response.ok ? response.json() : null).then((jobs) => { if (sequence === previewSequence.current) setScheduledJob(jobs?.jobs?.find((item) => item.planFingerprint === data.planFingerprint) || null); }).catch(() => {}); } }}>Preview bounded evidence or reopen saved review</button>
      {preview && <div className="border p-3" style={{ borderColor: C.border }}>
        <EvidenceSuitability suitability={preview.suitability} />
        {preview.plannedCalls !== 1 ? <p role="status" style={{ color: C.amber }}>{Object.hasOwn(UNAVAILABLE, preview.unavailableReason) ? UNAVAILABLE[preview.unavailableReason] : "This selection has incomplete evidence. Inspect the recorded facts locally or choose another scope."} No evidence was sent.</p> : <>
        <p>{preview.evidencePreview?.coverageBasis === "recorded_text_only" ? "Recorded text only · source events may be incomplete" : `Structural capture: ${preview.coverage}`} · {preview.provider}/{preview.model}</p>
        <p style={{ color: C.sub }}>Citation linkage does not verify factual support or entailment. Maximum output allowance: {preview.maximumOutputTokens} tokens.</p>
        <p style={{ color: C.sub }}>Destination: {preview.destination}. Conservative static list-price bound: {preview.upperStaticListPriceUsd == null ? "unavailable" : `$${preview.upperStaticListPriceUsd.toFixed(4)}`}; actual billing may differ.</p>
        <details className="mt-2"><summary>Review exact redacted evidence</summary><pre className="whitespace-pre-wrap break-words max-h-72 overflow-auto mt-2">{JSON.stringify(preview.evidencePreview, null, 2)}</pre></details>
        {!current && <p style={{ color: C.amber }}>The selection or check changed. Preview again.</p>}
        {preview.savedResult ? <p role="status" style={{ color: C.green }}>Saved review for this exact evidence and check. No new judge call is needed.</p> : <>
        <label>Maximum approved static estimate (USD)<input className="block border p-2 mt-1 w-32 bg-transparent" type="number" min="0.001" max="100" step="0.001" value={estimateLimit} onChange={(event) => setEstimateLimit(event.target.value)} /></label>
        <label className="flex gap-2 mt-3"><input type="checkbox" aria-label="Approve one judge call" disabled={!current || preview.plannedCalls !== 1} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I approve sending this exact bounded evidence and check to {preview.destination} for one judge call.</label>
        <button type="button" className="border px-3 py-2 mt-2" disabled={!current || !confirmed || !configured || !withinEstimateLimit || busy || preview.plannedCalls !== 1} onClick={async () => { const data = await post("/api/scoped/run", { ...config, planFingerprint: preview.planFingerprint, maxEstimatedUsd: approvedEstimate, confirmExternalEgress: true }); if (data) setResult(data); }}>Run evaluation</button>
        {!configured && <p style={{ color: C.amber }}>Provider key is not configured for this dashboard process.</p>}
        </>}
        {current && preview.plannedCalls === 1 && ["anthropic", "openai"].includes(provider) && (!preview.savedResult || scheduledJob) && <div className="border-t pt-3 mt-3 space-y-2" style={{ borderColor: C.border }}>
          <div className="font-semibold">Queue one evaluation for the next local service cycle</div>
          {!preview.savedResult && <>
            <p style={{ color: C.sub }}>The exact evidence and check above are authorized once. If evidence changes before the worker runs, the approval is cancelled. An interrupted or timed-out attempt is never retried automatically.</p>
            <p>Conservative static list-price bound: {preview.scheduledUpperStaticListPriceUsd == null ? "unavailable" : `$${preview.scheduledUpperStaticListPriceUsd.toFixed(4)}`}. Actual provider billing may differ.</p>
            <label className="flex gap-2"><input type="checkbox" aria-label="Approve scheduled judge call" checked={scheduleConfirmed} onChange={(event) => setScheduleConfirmed(event.target.checked)} />I authorize this exact evidence and check to be sent to {preview.destination} once during a later local service cycle, within the static estimate limit above.</label>
            {scheduledJob?.state !== "pending" && <button type="button" className="border px-3 py-2" disabled={!scheduleConfirmed || busy || !withinEstimateLimit} onClick={async () => { const data = await post("/api/scoped/schedule", { ...config, planFingerprint: preview.planFingerprint, maxEstimatedUsd: approvedEstimate, confirmPersistentExternalEgress: true }); if (data) { setScheduledJob(data); setScheduleConfirmed(false); } }}>Queue one evaluation</button>}
          </>}
          {scheduledJob && <div role="status">Scheduled approval: {scheduledJob.state}{scheduledJob.outcome ? ` · ${scheduledJob.outcome.replaceAll("_", " ")}` : ""}{scheduledJob.resultId ? ` · saved result ${scheduledJob.resultId.slice(0, 12)}` : ""}{scheduledJob.error ? ` · ${scheduledJob.error}` : ""}. {scheduledJob.resultId && <button type="button" className="underline ml-2" onClick={async () => { try { const response = await fetch(`${root}/api/scoped/result/${encodeURIComponent(scheduledJob.resultId)}`, { credentials: "same-origin" }); if (!response.ok) throw new Error(`HTTP ${response.status}`); setResult(await response.json()); setError(null); } catch (failure) { setError(String(failure)); } }}>Open saved result</button>}{scheduledJob.state === "pending" && <button type="button" className="underline ml-2" disabled={busy} onClick={async () => { const data = await post(`/api/scoped/scheduled/${scheduledJob.jobId}/cancel`, { expectedRevision: scheduledJob.revision }); if (data) { setScheduledJob({ ...scheduledJob, ...data }); setScheduleConfirmed(false); } }}>Cancel approval</button>}</div>}
        </div>}
        </>}
        {scope === "turn_window" && !textOnly && ["source_capture_partial", "turn_content_partial"].includes(preview.unavailableReason) && <button type="button" className="border px-3 py-2 mt-2" onClick={() => { ++previewSequence.current; setTextOnly(true); setPreview(null); setPreviewKey(null); setConfirmed(false); setScheduleConfirmed(false); setScheduledJob(null); setResult(null); }}>Review recorded text only…</button>}
      </div>}
      {result && current && <div className="border p-3" style={{ borderColor: C.border }}>
        <p>Evaluation {result.status} · evidence {result.coverage} · evaluator {result.evaluatorFingerprint}</p>
        {result.evidenceStatus === "stale" && <p role="status" style={{ color: C.amber }}>Evidence changed. This saved assessment is historical and has not been rewritten.</p>}
        {result.dimensions?.map((item) => <div key={item.name} className="mt-2">
          <p>{item.name}: {item.verdict}{["linked", "linked_unverified_support"].includes(item.citationStatus) ? " · Linked evidence; support unverified" : " · Uncited judge opinion"} · {item.explanation} {item.evidenceReferences?.join(", ")}</p>
          <p style={{ color: C.sub }}>Criterion: {result.criterionDescriptions?.[item.name] || "Exact criterion wording unavailable for this older result"}</p>
        </div>)}
        {result.turnAssessments && <div className="mt-3 space-y-3">
          <p>Historical audit · original-turn evidence and later retrospective evidence are assessed separately. Judge conclusions remain unvalidated.</p>
          {result.turnPairs?.map((pair) => <div key={pair.reference} className="border p-2" style={{ borderColor: C.border }}>
            <p className="break-words">{pair.reference} · {pair.startedAt || "time unknown"} → {pair.endedAt || "time unknown"}</p>
            <p>Request: {pair.request}</p><p>Response: {pair.response}</p>
            <p style={{ color: C.sub }}>Capture: request {pair.capture?.request}; response {pair.capture?.response}; sources {pair.capture?.sources}.</p>
            {result.turnAssessments.filter((item) => item.turnReference === pair.reference).map((item) => <div key={item.dimension} className="mt-2">
              <p>{item.dimension} · Original: {item.original.verdict} · {item.original.reason}</p>
              <p>Retrospective: {item.retrospective.verdict} · {item.retrospective.reason}</p>
              <details><summary>Assessment citations and evidence availability</summary><pre className="whitespace-pre-wrap break-words">{JSON.stringify({ original: item.original.citations, retrospective: item.retrospective.citations, availableOriginally: pair.originalEvidenceReferences, laterEvidence: pair.laterEvidenceReferences, uncertainAvailability: pair.unplacedEvidenceReferences }, null, 2)}</pre></details>
            </div>)}
          </div>)}
          {result.historicalFindings?.map((finding) => <div key={finding.id} className="border p-2" style={{ borderColor: C.border }}>
            <p>Historical finding {finding.id} · {finding.dimension} · {finding.reason}</p>
            <p>Recovery for this finding: {finding.recovery.state} · {finding.recovery.reason}</p>
            <details><summary>Finding and recovery evidence</summary><pre className="whitespace-pre-wrap break-words">{JSON.stringify(finding, null, 2)}</pre></details>
          </div>)}
        </div>}
        {result.error && <p style={{ color: C.red }}>Judge result unavailable: {result.error}</p>}
      </div>}
      {error && <p role="alert" style={{ color: C.red }}>{error}</p>}
    </div>}
  </div>;
}
