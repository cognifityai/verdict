import React, { useEffect, useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, RefreshCw, Shield, Signal } from "lucide-react";

const color = { panel: "#111715", border: "#26332e", sub: "#94a39d", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };
const panel = { background: color.panel, borderColor: color.border };

export function ControlCenter({ configUrl, onNavigate = null }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const [token, setToken] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [schedule, setSchedule] = useState({ intervalHours: 24, claudeRoot: "~/.claude/projects", codexRoot: "~/.codex/sessions", runMonitor: true });
  const [alert, setAlert] = useState({ destination: "local_log", webhookUrlEnvVar: "", findings: true, drift: true });
  const [settings, setSettings] = useState({ captureContent: true, retentionDays: 90, providerKeyEnvVars: ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"] });
  const [proposal, setProposal] = useState({ category: "policy", title: "", summary: "" });

  const load = React.useCallback(async () => {
    setError(null);
    try {
      const [authResponse, stateResponse] = await Promise.all([
        fetch(`${root}/api/setup/token`, { credentials: "same-origin" }),
        fetch(`${root}/api/control`, { credentials: "same-origin" }),
      ]);
      if (!authResponse.ok || !stateResponse.ok) throw new Error("Control center unavailable");
      const [auth, state] = await Promise.all([authResponse.json(), stateResponse.json()]);
      setToken(auth.setupToken); setData(state);
    } catch (failure) { setError(String(failure)); }
  }, [root]);
  useEffect(() => { load(); }, [load]);

  const documents = data?.documents || [];
  const current = React.useCallback((kind, id) => documents.find((item) => item.kind === kind && item.documentId === id), [documents]);
  useEffect(() => {
    const savedSchedule = current("schedule", "daily");
    const savedAlert = current("alert", "default");
    const savedSettings = current("settings", "default");
    if (savedSchedule) setSchedule(savedSchedule.payload);
    if (savedAlert) setAlert(savedAlert.payload);
    if (savedSettings) setSettings(savedSettings.payload);
  }, [current]);

  async function post(path, body, name) {
    setBusy(name); setError(null);
    try {
      const response = await fetch(`${root}${path}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      await load(); return result;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(null); }
  }
  const save = (kind, id, state, payload) => post(`/api/control/${kind}/${id}`, {
    state, payload, expectedRevision: current(kind, id)?.revision || null,
  }, `${kind}:${id}`);
  const proposals = documents.filter((item) => item.kind === "proposal");
  const reviews = new Set(documents.filter((item) => item.kind === "review" && item.state === "resolved").map((item) => item.documentId));
  const openReviews = (data?.reviewQueue || []).filter((item) => !reviews.has(reviewId(item)));
  const signalText = Object.entries(data?.userSignals?.counts || {}).map(([name, count]) => `${name}: ${count}`).join(" · ") || "No user signals captured";

  return <div className="max-w-6xl space-y-5">
    {error && <div role="alert" className="border p-4" style={{ ...panel, color: color.red }}><AlertTriangle size={15} className="inline mr-2" />{error}</div>}
    <section className="border p-5" style={panel}>
      <div className="flex justify-between gap-3"><div><div className="text-xs font-mono" style={{ color: color.green }}>DAILY OPERATIONS</div><h2 className="text-lg font-semibold mt-1">Rescan, analyze and notify</h2></div><button onClick={load} className="border p-2" style={{ borderColor: color.border }}><RefreshCw size={14} /></button></div>
      <div className="grid sm:grid-cols-3 gap-3 mt-4">
        <Field label="Interval hours"><input type="number" min="1" max="168" value={schedule.intervalHours} onChange={(event) => setSchedule({ ...schedule, intervalHours: Number(event.target.value) })} /></Field>
        <Field label="Claude history"><input value={schedule.claudeRoot} onChange={(event) => setSchedule({ ...schedule, claudeRoot: event.target.value })} /></Field>
        <Field label="Codex history"><input value={schedule.codexRoot} onChange={(event) => setSchedule({ ...schedule, codexRoot: event.target.value })} /></Field>
      </div>
      <label className="flex gap-2 text-sm mt-3"><input type="checkbox" checked={schedule.runMonitor} onChange={(event) => setSchedule({ ...schedule, runMonitor: event.target.checked })} />Run the active monitor after each content-on rescan</label>
      <div className="flex flex-wrap gap-2 mt-4"><Button busy={busy === "schedule:daily"} onClick={() => save("schedule", "daily", "active", schedule)}>Save schedule</Button><Button busy={busy === "run"} onClick={() => post("/api/control/actions/run-schedule", schedule, "run")}><ArrowRight size={13} />Run cycle now</Button></div>
      <p className="text-xs mt-3" style={{ color: color.sub }}>Unlike one-time manual preview approval, the saved daily schedule intentionally retains these local source paths as durable configuration. Use <code>verdict-service</code> for a continuously running worker; this button executes the same capture → persisted analysis → optional monitor cycle once.</p>
    </section>

    <div className="grid lg:grid-cols-2 gap-5">
      <section className="border p-5" style={panel}><Heading icon={Signal} title="Alert destinations" />
        <Field label="Destination"><select value={alert.destination} onChange={(event) => setAlert({ ...alert, destination: event.target.value })}><option value="local_log">Local finding log</option><option value="webhook">Webhook from environment variable</option></select></Field>
        {alert.destination === "webhook" && <Field label="Webhook URL environment variable"><input value={alert.webhookUrlEnvVar || ""} onChange={(event) => setAlert({ ...alert, webhookUrlEnvVar: event.target.value })} placeholder="VERDICT_ALERT_WEBHOOK_URL" /></Field>}
        <Button busy={busy === "alert:default"} onClick={() => save("alert", "default", "active", alert)}>Save destination</Button>
        <div className="mt-4 space-y-2">{(data?.notifications || []).map((item, index) => <div key={index} className="border p-3 text-sm" style={{ borderColor: color.amber }}>{item.kind}: {item.message}</div>)}{!data?.notifications?.length && <Small>No active finding notifications.</Small>}</div>
        <div className="mt-4"><div className="text-xs font-mono" style={{ color: color.sub }}>DELIVERY HISTORY</div><div className="mt-2 space-y-2">{(data?.deliveryAttempts || []).slice(0, 20).map((attempt) => <div key={attempt.attemptId} className="border p-3 text-xs" style={{ borderColor: attempt.outcome === "failed" ? color.red : color.border }}><span style={{ color: attempt.outcome === "failed" ? color.red : color.green }}>{attempt.outcome}</span> · {attempt.sourceKind} · {attempt.attemptedAt}{attempt.httpStatus ? ` · HTTP ${attempt.httpStatus}` : ""}{attempt.errorCode ? ` · ${attempt.errorCode}` : ""}</div>)}{!data?.deliveryAttempts?.length && <Small>No delivery attempts recorded.</Small>}</div></div>
      </section>
      <section className="border p-5" style={panel}><Heading icon={Shield} title="Privacy, retention and secrets" />
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={settings.captureContent} disabled />Bounded redacted content capture is on by default</label>
        <Field label="Retention days"><input type="number" min="1" max="3650" value={settings.retentionDays || ""} onChange={(event) => setSettings({ ...settings, retentionDays: Number(event.target.value) })} /></Field>
        <Field label="Provider key environment-variable names"><input value={(settings.providerKeyEnvVars || []).join(",")} onChange={(event) => setSettings({ ...settings, providerKeyEnvVars: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} /></Field>
        <Small>Verdict stores names and availability, never key values. Retention is a policy setting; pruning remains an explicit operation.</Small>
        <Button busy={busy === "settings:default"} onClick={() => save("settings", "default", "active", settings)}>Save settings</Button>
      </section>
    </div>

    <section className="border p-5" style={panel}><Heading icon={CheckCircle2} title="Review queue and user signals" />
      <div className="text-sm" style={{ color: color.sub }}>{signalText}</div>
      <div className="mt-4 space-y-2">{openReviews.slice(0, 20).map((item) => <div key={reviewId(item)} className="border p-3 flex flex-wrap items-center gap-3 text-sm" style={{ borderColor: color.border }}><span className="font-mono">{item.traceId}</span><span>{item.dimension}: {item.verdict}</span><button className="ml-auto border px-3 py-1" onClick={() => save("review", reviewId(item), "resolved", { traceId: item.traceId, judgmentId: item.judgmentId, dimension: item.dimension, label: item.verdict })}>Confirm label</button></div>)}{!openReviews.length && <Small>No unresolved fail/unclear judge suggestions in the bounded queue.</Small>}</div>
    </section>

    <section className="border p-5" style={panel}><Heading icon={CheckCircle2} title="Change-request decision log" />
      <Small>These records document a human decision; approving one does not deploy a policy, taxonomy, evaluator, cluster version, or baseline. Use the typed workflow linked from each record to make an actual product change.</Small>
      <div className="grid sm:grid-cols-3 gap-3"><Field label="Category"><select value={proposal.category} onChange={(event) => setProposal({ ...proposal, category: event.target.value })}>{["policy", "taxonomy", "evaluator", "baseline"].map((value) => <option key={value}>{value}</option>)}</select></Field><Field label="Title"><input value={proposal.title} onChange={(event) => setProposal({ ...proposal, title: event.target.value })} /></Field><Field label="Why"><input value={proposal.summary} onChange={(event) => setProposal({ ...proposal, summary: event.target.value })} /></Field></div>
      <Button disabled={!proposal.title || !proposal.summary} busy={busy?.startsWith("proposal:")} onClick={() => { const id = `change-${Date.now()}`; save("proposal", id, "pending", proposal).then(() => setProposal({ category: "policy", title: "", summary: "" })); }}>Create proposal</Button>
      <div className="mt-4 space-y-2">{proposals.map((item) => <div key={item.documentId} className="border p-3 text-sm" style={{ borderColor: color.border }}><div className="font-semibold">{item.payload.title}</div><div className="mt-1" style={{ color: color.sub }}>{item.payload.category} · decision {item.state} · revision {item.revision}</div><div className="mt-1">{item.payload.summary}</div><div className="flex flex-wrap gap-2 mt-3">{item.state === "pending" && <><Button onClick={() => save("proposal", item.documentId, "approved", item.payload)}>Record approval</Button><Button onClick={() => save("proposal", item.documentId, "rejected", item.payload)}>Record rejection</Button></>}{item.revision > 1 && <Button onClick={() => post(`/api/control/proposal/${item.documentId}/rollback`, { targetRevision: item.revision - 1, expectedRevision: item.revision }, `rollback:${item.documentId}`)}><RefreshCw size={13} />Revert decision record</Button>}<Button onClick={() => onNavigate?.(workflowFor(item.payload.category))}>Open typed workflow</Button></div></div>)}</div>
    </section>
  </div>;
}

function reviewId(item) { return `${item.judgmentId}:${item.dimension}`.replace(/[^A-Za-z0-9._:-]/g, "-").slice(0, 128); }
function workflowFor(category) {
  if (category === "evaluator") return { tab: "evaluators" };
  if (category === "taxonomy") return { tab: "drift", section: "clusters" };
  return { tab: "drift", section: category === "baseline" ? "explore" : "monitor" };
}
function Heading({ icon: Icon, title }) { return <h2 className="font-semibold flex items-center gap-2 mb-4"><Icon size={15} style={{ color: color.green }} />{title}</h2>; }
function Field({ label, children }) { return <label className="block text-sm mb-3">{label}{React.cloneElement(children, { className: "block w-full mt-1 border p-2 bg-transparent", style: { borderColor: color.border } })}</label>; }
function Button({ children, onClick, busy, disabled }) { return <button disabled={busy || disabled} onClick={onClick} className="inline-flex items-center gap-2 border px-3 py-2 text-sm" style={{ borderColor: color.green, color: disabled ? color.sub : color.green }}>{busy ? "Working…" : children}</button>; }
function Small({ children }) { return <p className="text-xs my-3" style={{ color: color.sub }}>{children}</p>; }
