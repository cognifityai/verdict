import React, { useRef, useState } from "react";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };

function StructuredResult({ result }) {
  const { output, computed } = result;
  return <div className="mt-3 space-y-2 border-t pt-3" style={{ borderColor: C.border }}>
    <div className="font-semibold">{computed.route === "alternate" ? "Alternate route" : "Standard route"} · {computed.overall}/100 · {computed.label}</div>
    {computed.route === "standard" ? <>
      <p>Weighted score {computed.raw_weighted} · safety gate {computed.gate ? "activated" : "not activated"} · confidence {computed.aggregate_confidence}%</p>
      <div className="grid sm:grid-cols-2 gap-2">{Object.entries(computed.categories).map(([name, category]) =>
        <div key={name} className="border p-2" style={{ borderColor: C.border }}>{name}: {category.assessed ? category.score : "not assessed"} · confidence {category.confidence}
          {computed.review_categories.includes(name) && <span style={{ color: C.amber }}> · review needed</span>}</div>
      )}</div>
      <p>Indices: {Object.entries(computed.indices).map(([name, score]) => `${name} ${score}`).join(" · ")}</p>
      {Object.entries(output.categories).map(([name, category]) => <details key={name} className="border p-2" style={{ borderColor: C.border }}>
        <summary className="cursor-pointer">{name} · {category.elements.length} element findings</summary>
        <div className="mt-2 space-y-2">{category.elements.map((item) => <div key={`${item.phase}:${item.element}`} className="border-t pt-2" style={{ borderColor: C.border }}>
          <strong>{item.phase} / {item.element}: {item.applicable ? item.adequacy : "not applicable"}</strong><p>{item.description}</p>
          {item.quote && <p dir="auto">Message {item.message_position + 1}: “{item.quote}”</p>}
        </div>)}</div>
      </details>)}
    </> : <>
      <p>Context: {output.alternate.context}</p>
      {Object.entries(output.alternate.scores).map(([name, score]) =>
        <p key={name}>{name}: {score}/5 · {output.alternate.score_reasons[name]}</p>)}
      <p>{output.alternate.adequacy}: {output.alternate.rationale}</p>
      {output.alternate.critical_flags.length > 0 && <p>Flags: {output.alternate.critical_flags.join(" · ")}</p>}
    </>}
  </div>;
}

export function ConversationEvaluation({ root, token, provider, model, providerState, updatePreferences, changeProvider }) {
  const [rubric, setRubric] = useState(null);
  const [maxCalls, setMaxCalls] = useState(10);
  const [maxOutputTokens, setMaxOutputTokens] = useState(4096);
  const [after, setAfter] = useState(null);
  const [preview, setPreview] = useState(null);
  const [previewKey, setPreviewKey] = useState(null);
  const [review, setReview] = useState(null);
  const [detail, setDetail] = useState(null);
  const [result, setResult] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inFlight = useRef(false);
  const providerSupported = ["anthropic", "openai", "google"].includes(provider);
  const config = (cursor = after) => ({ unit: "conversation", provider, model, rubric,
    maxCalls, maxOutputTokens, scanLimit: 20, ...(cursor ? { after: cursor } : {}) });
  const current = previewKey === JSON.stringify(config());

  async function request(path, options = {}) {
    const response = await fetch(`${root}${path}`, {
      credentials: "same-origin", ...options,
      headers: { "Content-Type": "application/json", "X-Verdict-Setup": token, ...options.headers },
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  }

  async function loadPage(cursor = null) {
    if (inFlight.current || !rubric || !token || !providerSupported) return;
    inFlight.current = true; setBusy(true); setError(null); setDetail(null); setResult(null); setConfirmed(false);
    try {
      const selected = config(cursor);
      const body = JSON.stringify(selected);
      const nextPreview = await request("/api/evaluators/preview", { method: "POST", body });
      const nextReview = await request("/api/data/conversations/review", { method: "POST", body });
      setAfter(cursor); setPreview(nextPreview); setReview(nextReview);
      setPreviewKey(JSON.stringify(selected)); setConfirmed(false);
    } catch (failure) { setError(String(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function upload(event) {
    const file = event.target.files?.[0];
    if (!file || !token || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null); setConfirmed(false);
    try {
      if (file.size > 128000) throw new Error("Rubric exceeds 128 KB");
      const document = JSON.parse(await file.text());
      const validated = await request("/api/evaluators/rubric/validate", {
        method: "POST", body: JSON.stringify({ document }),
      });
      setRubric(validated); setAfter(null); setPreview(null); setReview(null);
      setMaxOutputTokens(validated.kind === "element_scoring_v1" ? 16384 : 4096);
      setDetail(null); setResult(null); setConfirmed(false);
    } catch (failure) { setError(String(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function run() {
    if (inFlight.current || !providerSupported || !current || !confirmed || !preview?.plannedCalls) return;
    inFlight.current = true; setBusy(true); setError(null); setConfirmed(false);
    try {
      const approved = { ...config(), plannedTargets: preview.plannedTargets,
        planFingerprint: preview.planFingerprint, confirmExternalEgress: true };
      const outcome = await request("/api/evaluators/run", {
        method: "POST", body: JSON.stringify(approved),
      });
      setResult(outcome); setConfirmed(false);
      const refreshedPreview = await request("/api/evaluators/preview", {
        method: "POST", body: JSON.stringify(config()),
      });
      const refreshed = await request("/api/data/conversations/review", {
        method: "POST", body: JSON.stringify(config()),
      });
      setPreview(refreshedPreview);
      setReview(refreshed);
    } catch (failure) { setError(String(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function open(id) {
    if (inFlight.current || !preview) return;
    inFlight.current = true; setBusy(true); setError(null); setDetail(null);
    try {
      const value = await request(`/api/data/conversations/${encodeURIComponent(id)}?evaluator=${encodeURIComponent(preview.evaluatorFingerprint)}`);
      setDetail(value);
    } catch (failure) { setError(String(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  const destination = providerState?.customEndpointConfigured
    ? provider === "anthropic" ? "the configured Anthropic endpoint"
      : "the configured OpenAI-compatible endpoint"
    : provider;
  return <fieldset disabled={busy} className="max-w-5xl space-y-4" style={{ border: 0, margin: 0, padding: 0 }}>
    <section className="border p-5 space-y-4" style={{ borderColor: C.border, background: C.panel }}>
      <div className="text-xs font-mono" style={{ color: C.green }}>EVALUATOR LAB · CONVERSATIONS</div>
      <h2 className="text-lg font-semibold">Grade a whole conversation or each completed reply</h2>
      <p className="text-sm" style={{ color: C.sub }}>Import text transcripts through Setup → Existing telemetry → Voice. Only clean, closed conversations with a completed reply are graded. Preview makes no judge call.</p>
      <div className="grid sm:grid-cols-2 gap-4">
        <label className="text-sm">Evaluation unit<select value="conversation" onChange={e => updatePreferences({ unit: e.target.value })} className="block w-full border p-2 mt-1 bg-transparent"><option value="conversation">Conversation or reply</option><option value="trace">Provider Trace</option><option value="agent_turn">Agent Turn</option></select></label>
        <label className="text-sm">Provider<select value={provider} onChange={e => changeProvider(e.target.value)} className="block w-full border p-2 mt-1 bg-transparent">{!providerSupported && <option value={provider} disabled>{provider} (Trace/Turn only)</option>}{["anthropic", "openai", "google"].map(name => <option key={name} value={name}>{name === "openai" ? "openai / compatible endpoint" : name}</option>)}</select></label>
        <label className="text-sm">Judge model<input value={model} onChange={e => updatePreferences({ model: e.target.value })} className="block w-full border p-2 mt-1 bg-transparent" /></label>
        <label className="text-sm">Maximum calls in this page<input type="number" min="1" max="20" value={maxCalls} onChange={e => setMaxCalls(Number(e.target.value))} className="block w-full border p-2 mt-1 bg-transparent" /></label>
        <label className="text-sm">Maximum judge output tokens<input type="number" min="256" max="32768" value={maxOutputTokens} onChange={e => setMaxOutputTokens(Number(e.target.value))} className="block w-full border p-2 mt-1 bg-transparent" /></label>
      </div>
      {!providerSupported && <p className="text-sm" style={{ color: C.amber }}>Jev cannot grade conversations. Select Anthropic, OpenAI, or Google before preview.</p>}
      <label className="block text-sm font-semibold">Rubric JSON file<input type="file" accept=".json,application/json" aria-label="Rubric JSON file" onChange={upload} className="block w-full mt-2" /></label>
      {rubric && <div className="border p-3 text-sm" style={{ borderColor: C.border }}><strong>{rubric.name} · v{rubric.version}</strong> · {rubric.target}
        {rubric.kind === "element_scoring_v1" ? <div className="mt-1">Element scoring · {Object.values(rubric.catalog).reduce((count, elements) => count + elements.length, 0)} declared elements · standard and alternate routes. The judge reports findings; Verdict calculates scores and the safety gate.
          <div>{Object.entries(rubric.scoring.weights).map(([name, weight]) => `${name} ${Math.round(weight * 100)}%`).join(" · ")}</div>
          {Object.values(rubric.catalog).some(elements => elements.some(item => item.phase !== "general")) &&
            <div>Voice records must include source enabled_phases for this rubric.</div>}
        </div> : <div className="mt-1">Simple dimensions · {rubric.dimensions.map(d => `${d.name} (${d.type === "number" ? `${d.min}–${d.max}` : "PASS / FAIL / UNCLEAR"})`).join(" · ")}</div>}
        <div className="font-mono text-xs break-all mt-2" style={{ color: C.sub }}>{rubric.fingerprint}</div></div>}
      <button disabled={!rubric || !token || !providerSupported} onClick={() => loadPage(null)} className="border px-4 py-2 text-sm">Preview first page</button>
    </section>
    {preview && <section className="border p-5 space-y-3" style={{ borderColor: C.border, background: C.panel }}>
      <h3 className="font-semibold">Preview · {preview.target} rubric</h3>
      <p className="text-xs font-mono break-all" style={{ color: C.sub }}>Evaluator fingerprint for Monitor: {preview.evaluatorFingerprint}</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">{[["Conversations scanned", preview.scannedConversations], ["Eligible targets", preview.eligibleTargets], ["Already graded", preview.alreadyJudged], ["Planned calls", preview.plannedCalls]].map(([label, value]) => <div key={label} className="border p-3" style={{ borderColor: C.border }}><div style={{ color: C.sub }}>{label}</div><strong>{value}</strong></div>)}</div>
      <p className="text-sm" style={{ color: C.sub }}>Not evaluable: {Object.entries(preview.notEvaluableReasons).map(([reason, count]) => `${reason}: ${count}`).join(" · ") || "none"}. Retryable judge errors: {preview.retryableErrors}.</p>
      {preview.alternateOnlyTargets > 0 && <p className="text-sm" style={{ color: C.amber }}>{preview.alternateOnlyTargets} target(s) have no source enabled phases. Only the alternate route can be graded; standard judgments will be rejected.</p>}
      {!current && <p className="text-sm" style={{ color: C.amber }}>Configuration changed. Preview again before running.</p>}
      <label className="flex gap-2 text-sm"><input type="checkbox" disabled={!current} checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />I approve sending {preview.plannedCalls} selected redacted {preview.target === "response" ? "replies with preceding messages" : "full conversations"} to {destination}. Redaction is best effort.</label>
      <button disabled={!current || !confirmed || !providerState?.configured || !preview.plannedCalls} onClick={run} className="px-4 py-2 text-sm" style={{ background: C.green, color: "#0b0e0d" }}>Run {preview.plannedCalls} judge calls</button>
      {result && <p role="status" className="text-sm">Saved: {result.completed} · judge errors: {result.errors} · changed during run: {result.stale}</p>}
    </section>}
    {review && <section className="border p-5 space-y-3" style={{ borderColor: C.border, background: C.panel }}>
      <h3 className="font-semibold">Conversation evidence</h3>
      <p className="text-xs" style={{ color: C.sub }}>This page is ordered by opaque conversation ID, not by time. Coverage uses the exact rubric, evaluator and current transcript revision.</p>
      <div className="overflow-auto max-h-96"><table className="w-full text-sm text-left"><thead><tr><th className="p-2">Conversation</th><th className="p-2">Ending</th><th className="p-2">Coverage</th></tr></thead><tbody>{review.conversations.map(row => <tr key={row.id} className="border-t" style={{ borderColor: C.border }}><td className="p-2"><button className="underline" onClick={() => open(row.id)}>{row.id.slice(0, 12)}</button><div className="text-xs" style={{ color: C.sub }}>{row.event_at || "No source time"}</div></td><td className="p-2">{row.end_status}{row.input_issues.length > 0 && <div style={{ color: C.amber }}>{row.input_issues.join(" · ")}</div>}</td><td className="p-2">{row.coverage.ineligibleReason || `${row.coverage.completed}/${row.coverage.targets} completed · ${row.coverage.error} errors · ${row.coverage.missing} missing`}{row.coverage.fullyGraded && <div style={{ color: C.green }}>Fully graded</div>}</td></tr>)}</tbody></table></div>
      {review.nextCursor && <button onClick={() => loadPage(review.nextCursor)} className="border px-3 py-2 text-sm">Next page by ID</button>}
    </section>}
    {detail && <section className="border p-5 space-y-3" style={{ borderColor: C.border, background: C.panel }}>
      <div className="flex justify-between"><h3 className="font-semibold">Messages and grades</h3><button className="underline text-sm" onClick={() => setDetail(null)}>Close</button></div>
      <p className="font-mono text-xs break-all" style={{ color: C.sub }}>Revision {detail.conversation.revision}</p>
      {detail.conversation.enabled_phases && <p className="text-xs" style={{ color: C.sub }}>Source enabled phases: {detail.conversation.enabled_phases.join(" · ") || "none"}</p>}
      {detail.assessments.map(a => <div key={a.id} className="border p-3 text-sm" style={{ borderColor: C.border }}><strong>{a.rubric.name} v{a.rubric.version}</strong> · {a.target_position == null ? "whole conversation" : `reply at message ${a.target_position + 1}`} · {a.status}
        {a.structured ? <StructuredResult result={a.structured} /> : <><div>{Object.entries(a.dimensions).map(([name, value]) => <p key={name}>{name}: {value.score == null ? value.state : value.score} · {value.reason}</p>)}</div>{a.findings.map((finding, i) => <p key={i}>{finding.issue}: {finding.reason}{finding.quote && <span> · “{finding.quote}”</span>}</p>)}</>}
      </div>)}
      <div className="space-y-2 max-h-96 overflow-auto">{detail.conversation.messages.map((m, i) => <div key={i} className="border p-3 text-sm" style={{ borderColor: C.border }}><strong>{i + 1} · {m.role} · {m.status}</strong><p dir="auto" className="whitespace-pre-wrap mt-1">{m.content}</p></div>)}</div>
    </section>}
    {error && <p role="alert" className="border p-3 text-sm" style={{ borderColor: C.red, color: C.red }}>{error}</p>}
  </fieldset>;
}
