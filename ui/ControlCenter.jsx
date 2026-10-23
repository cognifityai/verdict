import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, RefreshCw, Shield, Signal } from "lucide-react";
import { normalizeSchedulePayload, schedulePlanKey } from "./schedule-payload.js";
import { cycleVerifiedForPlan, expectedNextCheck } from "./guided-flow.mjs";
import { EvidenceExplain } from "./EvidenceExplain.jsx";

const color = { panel: "#111715", border: "#26332e", sub: "#94a39d", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };
const panel = { background: color.panel, borderColor: color.border };
const deliveryLabel = {
  pending: "Ready to send", unbound: "Waiting for webhook URL",
  failed: "Retry scheduled", delivered: "Delivered",
  blocked: "Destination needs review", exhausted: "Retries exhausted",
  superseded: "Replaced after review",
};
const workerTime = (value) => {
  if (typeof value !== "string" || !value) return "time unavailable";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric",
      year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(date)
    : "time unavailable";
};

export function ControlCenter({ configUrl, onNavigate = null, section = "all", meta = {}, monitor = null, embedded = false }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const [token, setToken] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [schedule, setSchedule] = useState({ intervalHours: 24,
    claudeRoot: "", codexRoot: "", analyzeOnly: true, runMonitor: false,
    includeApprovedEvaluations: false });
  const [alert, setAlert] = useState({ destination: "local_log", webhookUrlEnvVar: "", findings: true, drift: true });
  const [settings, setSettings] = useState({ captureContent: true, providerKeyEnvVars: ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"] });
  const [proposal, setProposal] = useState({ category: "policy", title: "", summary: "" });
  const [lastRun, setLastRun] = useState(null);
  const [lastRunPlanKey, setLastRunPlanKey] = useState(null);
  const [testedPlan, setTestedPlan] = useState(null);
  const [draftMessage, setDraftMessage] = useState(null);
  const [rebindPreview, setRebindPreview] = useState(null);
  const [deliveryExtra, setDeliveryExtra] = useState([]);
  const [deliveryCursor, setDeliveryCursor] = useState(null);
  const [deliveryHasMore, setDeliveryHasMore] = useState(null);
  const deliveryGeneration = useRef(0);
  const deliveryPageInFlight = useRef(false);
  const [showQueuedAlerts, setShowQueuedAlerts] = useState(false);
  const [showAllDeliveryProblems, setShowAllDeliveryProblems] = useState(false);

  const load = React.useCallback(async () => {
    const generation = ++deliveryGeneration.current;
    setError(null);
    try {
      const [authResponse, stateResponse] = await Promise.all([
        fetch(`${root}/api/setup/token`, { credentials: "same-origin" }),
        fetch(`${root}/api/control`, { credentials: "same-origin" }),
      ]);
      if (!authResponse.ok || !stateResponse.ok) throw new Error("Control center unavailable");
      const [auth, state] = await Promise.all([authResponse.json(), stateResponse.json()]);
      if (generation !== deliveryGeneration.current) return;
      deliveryGeneration.current += 1;
      setToken(auth.setupToken); setData(state); setDeliveryExtra([]);
      setDeliveryCursor(null); setDeliveryHasMore(null);
    } catch (failure) {
      if (generation === deliveryGeneration.current) setError(String(failure));
    }
  }, [root]);
  useEffect(() => { load(); }, [load]);

  const documents = data?.documents || [];
  const deliveryRows = [...new Map([...(data?.deliveryQueue || []), ...deliveryExtra]
    .map((item) => [item.intentId, item])).values()];
  const actionableDeliveryCount = ["pending", "unbound", "failed", "blocked", "exhausted"]
    .reduce((total, status) => total + (data?.deliveryQueueCounts?.[status] || 0), 0);
  const deliveryProblems = deliveryRows.filter((item) => ["failed", "blocked", "exhausted"].includes(item.status));
  const deliveryWaiting = deliveryRows.filter((item) => ["pending", "unbound"].includes(item.status));
  const waitingCount = (data?.deliveryQueueCounts?.pending || 0) + (data?.deliveryQueueCounts?.unbound || 0);
  async function loadMoreDeliveries() {
    if (deliveryPageInFlight.current) return;
    deliveryPageInFlight.current = true;
    const generation = deliveryGeneration.current;
    setBusy("delivery-page"); setError(null);
    try {
      const anchor = deliveryCursor || deliveryRows.at(-1);
      const query = new URLSearchParams();
      if (anchor) {
        query.set("afterCreatedAt", anchor.createdAt);
        query.set("afterIntentId", anchor.intentId);
      }
      const response = await fetch(`${root}/api/control/delivery${query.size ? `?${query}` : ""}`, {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`Delivery queue unavailable: HTTP ${response.status}`);
      const page = await response.json();
      if (generation !== deliveryGeneration.current) return;
      setDeliveryExtra((existing) => [...existing, ...page.deliveryQueue]);
      setDeliveryCursor(page.deliveryQueue.at(-1) || anchor || null);
      setDeliveryHasMore(page.hasMore === true);
    } catch (failure) {
      if (generation === deliveryGeneration.current) setError(String(failure));
    } finally {
      deliveryPageInFlight.current = false;
      setBusy((current) => current === "delivery-page" ? null : current);
    }
  }
  const current = React.useCallback((kind, id) => documents.find((item) => item.kind === kind && item.documentId === id), [documents]);
  useEffect(() => {
    const savedSchedule = current("schedule", "daily");
    const savedAlert = current("alert", "default");
    const savedSettings = current("settings", "default");
    if (savedSchedule) setSchedule({ ...savedSchedule.payload,
      includeApprovedEvaluations: savedSchedule.payload.includeApprovedEvaluations === true
        || (savedSchedule.state === "active" && savedSchedule.payload.includeApprovedEvaluations === undefined) });
    if (savedAlert) setAlert(savedAlert.payload);
    if (savedSettings) {
      const currentSettings = { ...savedSettings.payload };
      delete currentSettings.retentionDays;
      setSettings(currentSettings);
    }
  }, [current]);
  async function post(path, body, name, { reload = true } = {}) {
    setBusy(name); setError(null);
    try {
      const response = await fetch(`${root}${path}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      if (reload) await load();
      return result;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(null); }
  }
  const save = (kind, id, state, payload) => post(`/api/control/${kind}/${id}`, {
    state, payload, expectedRevision: current(kind, id)?.revision || null,
  }, `${kind}:${id}`);
  async function reviewDeliveryDestination(item) {
    setError(null);
    setRebindPreview(null);
    try {
      const response = await fetch(`${root}/api/control/delivery/${item.intentId}/rebind-preview`, {
        credentials: "same-origin", headers: { "X-Verdict-Setup": token },
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      setRebindPreview(result);
    } catch (failure) { setError(String(failure)); }
  }
  async function confirmDeliveryDestination() {
    if (!rebindPreview) return;
    const old = rebindPreview.oldIntent;
    const result = await post(`/api/control/delivery/${old.intentId}/rebind`, {
      expectedOldStatus: old.status,
      expectedAttemptCount: old.attemptCount,
      expectedAlertRevision: rebindPreview.currentAlertRevision,
      expectedNewFingerprint: rebindPreview.newDestinationFingerprint,
    }, `rebind:${old.intentId}`);
    if (result) setRebindPreview(null);
  }
  const proposals = documents.filter((item) => item.kind === "proposal");
  const reviews = new Set(documents.filter((item) => item.kind === "review" && item.state === "resolved").map((item) => item.documentId));
  const openReviews = (data?.reviewQueue || []).filter((item) => !reviews.has(reviewId(item)));
  const cyclePlan = normalizeSchedulePayload(schedule);
  const planKey = schedulePlanKey(cyclePlan);
  const savedSchedule = current("schedule", "daily");
  const scheduleSaved = savedSchedule?.state === "active";
  const schedulePaused = savedSchedule?.state === "disabled";
  const pausedLegacyJudgeChoice = schedulePaused && savedSchedule.payload.includeApprovedEvaluations === undefined;
  const workerRecent = data?.worker?.status === "recent";
  const currentWorkerCheck = scheduleSaved && data?.lastCycle?.trigger === "worker"
    && data.lastCycle.scheduleRevision === savedSchedule.revision
    && data.lastCycle.state === "completed";
  const nextCheck = expectedNextCheck(current("schedule", "daily"), data);
  const runCount = Number(meta.totalAgentRuns || 0);
  const callCount = Number(meta.monitorEligibleCalls || 0);
  const hasActiveMonitor = monitor?.active?.state === "active";
  const needsCapture = runCount === 0 && callCount === 0 && !savedSchedule
    && !cyclePlan.claudeRoot && !cyclePlan.codexRoot;

  const deliveryRow = (item) => <div key={item.intentId} className="border p-3 text-xs" style={{ borderColor: ["failed", "blocked", "exhausted"].includes(item.status) ? color.amber : color.border }}>
    <strong>{item.status === "pending" && !workerRecent ? "Waiting for worker" : deliveryLabel[item.status] || item.status}</strong> · {item.kind === "drift" ? "Model-call comparison" : "Agent finding"}{item.code ? ` · ${item.code}` : ""} · {item.destination} · {item.attemptCount} attempt{item.attemptCount === 1 ? "" : "s"}
    {item.lastErrorCode && ` · ${item.lastErrorCode}`}
    {item.nextDueAt && <div style={{ color: color.sub }}>Next attempt: {workerTime(item.nextDueAt)}</div>}
    {item.status === "unbound" && <div style={{ color: color.sub }}>Set a valid final HTTPS URL in the configured worker environment variable.</div>}
    <div className="mt-2 flex gap-3">
      {item.status === "exhausted" && <button type="button" className="underline" style={{ color: color.green }} disabled={!!busy} onClick={() => post(`/api/control/delivery/${item.intentId}/replay`, { expectedAttemptCount: item.attemptCount }, `replay:${item.intentId}`)}>Retry once with current destination</button>}
      {["blocked", "failed", "exhausted", "pending"].includes(item.status) && <button type="button" className="underline" style={{ color: color.green }} disabled={!!busy} onClick={() => reviewDeliveryDestination(item)}>Review new destination</button>}
    </div>
  </div>;

  return <div className="max-w-6xl space-y-5">
    {error && <div role="alert" className="border p-4" style={{ ...panel, color: color.red }}><AlertTriangle size={15} className="inline mr-2" />{error}</div>}
    {["all", "schedule"].includes(section) && <>
      {!embedded && <div>
        <div className="text-xs font-mono" style={{ color: color.green }}>SUGGESTED SETUP</div>
        <h2 className="text-2xl font-semibold mt-1">A monitoring plan for this workspace</h2>
        <p className="text-sm mt-2" style={{ color: color.sub }}>Review what Verdict captured, run a local check, then decide what should run again. Approved judge jobs are off for a new plan unless you opt in.</p>
      </div>}
      <div className="verdict-setup-steps" aria-label="Setup steps"><strong>1 · Review evidence</strong><span>2 · Choose checks</span><span>3 · Start and verify</span></div>
      {data && <div className="grid lg:grid-cols-[minmax(0,2fr)_minmax(250px,1fr)] gap-5 items-start">
        <div className="space-y-5">
          <section className="border p-5" style={panel}>
            <h3 className="font-semibold">What Verdict found</h3>
            <p className="text-sm mt-2" style={{ color: color.sub }}>{runCount.toLocaleString()} agent runs · {callCount.toLocaleString()} completed monitor-eligible model calls.</p>
            <div className="border-t mt-4 pt-2 text-sm" style={{ borderColor: color.border }}>
              <div className="verdict-setup-row"><span className="verdict-setup-number">1</span><div><strong>{runCount ? "Agent runs are available" : "No agent runs captured"}</strong><p style={{ color: color.sub }}>{runCount ? "Recorded turns, tools, tests and source-reported usage can support a local deterministic check." : "Connect or import an agent source to review execution issues and usage."}</p></div></div>
              <div className="verdict-setup-row"><span className="verdict-setup-number">2</span><div><strong>{callCount ? "Provider model calls are available" : "No eligible provider model calls"}</strong><p style={{ color: color.sub }}>{callCount ? "A statistical call comparison is separate from the local run check and requires a reviewed reference." : "Agent history alone may not contain the provider calls needed for a statistical model-call watch."}</p></div></div>
              {needsCapture && <div role="status"><strong>Capture evidence before running a check</strong><p style={{ color: color.sub }}>Connect an agent source or instrument model calls, then return to review this plan.</p><button type="button" className="underline mt-2" style={{ color: color.green }} onClick={() => onNavigate?.({ tab: "settings", section: "sources" })}>Set up capture →</button></div>}
              {(meta.agentRunSources || []).length > 0 && <div className="verdict-setup-row"><span className="verdict-setup-number">3</span><div><strong>Recorded source groups</strong><p style={{ color: color.sub }}>Use source labels to separate captured work. These are not inferred task or intent clusters.</p><ul className="mt-2 space-y-1">{meta.agentRunSources.map((item) => <li key={item.sourceKind}>{item.sourceKind}: {Number(item.runs || 0).toLocaleString()} run{Number(item.runs || 0) === 1 ? "" : "s"}</li>)}</ul></div></div>}
            </div>
          </section>
          <section className="border p-5" style={panel}>
            <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">Recommended checks</h3><span className="verdict-setup-tag">{cyclePlan.includeApprovedEvaluations ? "Optional approved judge job" : "No judge call selected"}</span></div>
            {needsCapture ? <p className="text-sm mt-3" style={{ color: color.sub }}>Capture agent runs or provider calls to reveal eligible checks for this workspace.</p> : <div className="mt-4 space-y-4 text-sm">
              {runCount > 0 && <div className="verdict-setup-check"><CheckCircle2 size={17} /><div><strong>Review recorded execution issues</strong><p style={{ color: color.sub }}>Check failed tests, tool errors, and other captured run evidence. This does not judge answer quality.</p></div></div>}
              {runCount > 0 && <div className="verdict-setup-check"><CheckCircle2 size={17} /><div><strong>Review captured token use</strong><p style={{ color: color.sub }}>Home shows reported turn or model-call totals where available and labels their coverage. Open a day to inspect the matching sessions or calls. Missing totals stay unknown; usage alone does not trigger a cost alert.</p></div></div>}
              <div className="verdict-setup-check"><CheckCircle2 size={17} /><div><strong>Watch model-call changes when ready</strong><p style={{ color: color.sub }}>{hasActiveMonitor ? "An active call policy can run during this cycle if selected below." : callCount ? "Preview eligible calls and the reference before activating a separate statistical monitor." : "Connect eligible application model calls before setting up a statistical call watch."}</p>
                {callCount > 0 && !hasActiveMonitor && <button type="button" className="underline mt-1" style={{ color: color.green }} onClick={() => onNavigate?.({ tab: "monitor", section: "history" })}>Review model-call comparison →</button>}
              </div></div>
            </div>}
            {!needsCapture && <div className="border-t mt-4 pt-3" style={{ borderColor: color.border }}>
            <p className="text-sm" style={{ color: color.sub }}>Want a second opinion? AI can draft a check from aggregate source eligibility. Review the context and cost before any provider call.</p>
            <EvidenceExplain root={root} scope="setup" onApplyDraft={(draft) => {
              if (draft.check === "capture_first") {
                onNavigate?.({ tab: "settings", section: "sources" });
              } else if (draft.check === "model_calls") {
                onNavigate?.({ tab: "monitor", section: "history" });
              } else {
                setSchedule((current) => ({ ...current, intervalHours: draft.intervalHours,
                  analyzeOnly: true, runMonitor: false }));
                setDraftMessage("AI draft filled the local check interval. Run once and review before saving a recurring schedule.");
              }
            }} />
            {draftMessage && <p role="status" className="text-sm mt-2" style={{ color: color.amber }}>{draftMessage}</p>}
            </div>}
          </section>
          <section className="border p-5" style={panel}>
            <h3 className="font-semibold">Choose what to run</h3>
            <p className="text-sm mt-2" style={{ color: color.sub }}>Leave these paths blank to analyze already captured activity. Enter a local history directory only if this check should also import or rescan it. A recorded source label does not identify a directory on this machine.</p>
            <div className="grid sm:grid-cols-2 gap-3 mt-4">
              <Field label="Claude Code history directory"><input value={schedule.claudeRoot || ""} placeholder="~/.claude/projects" onChange={(event) => setSchedule({ ...schedule, claudeRoot: event.target.value })} /></Field>
              <Field label="Codex history directory"><input value={schedule.codexRoot || ""} placeholder="~/.codex/sessions" onChange={(event) => setSchedule({ ...schedule, codexRoot: event.target.value })} /></Field>
            </div>
            <label className="flex gap-2 text-sm mt-2"><input type="checkbox" checked={schedule.analyzeOnly === true} onChange={(event) => setSchedule({ ...schedule, analyzeOnly: event.target.checked })} />Analyze already captured activity</label>
            <label className="flex gap-2 text-sm mt-3"><input type="checkbox" disabled={!hasActiveMonitor} checked={schedule.runMonitor === true} onChange={(event) => setSchedule({ ...schedule, runMonitor: event.target.checked })} />Run the active model-call monitor{!hasActiveMonitor && " (set up separately first)"}</label>
            <details className="mt-4 text-sm"><summary className="cursor-pointer">Cadence and advanced options</summary>
              <div className="mt-3 max-w-xs"><Field label="Hours between checks"><input type="number" min="1" max="168" value={schedule.intervalHours} onChange={(event) => setSchedule({ ...schedule, intervalHours: Number(event.target.value) })} /></Field></div>
              <label className="flex gap-2 text-sm mt-3"><input type="checkbox" checked={schedule.includeApprovedEvaluations === true} onChange={(event) => setSchedule({ ...schedule, includeApprovedEvaluations: event.target.checked })} />Run one separately approved pending scoped judge job per cycle</label>
              <p className="text-xs mt-2" style={{ color: color.sub }}>Opting in can send the approved bounded session evidence to its configured model provider and incur its approved cost. Leaving this off preserves the pending approval. Existing saved schedules without this choice retain their prior behavior until reviewed and changed.</p>
            </details>
            {pausedLegacyJudgeChoice && <p className="text-sm mt-3" style={{ color: color.amber }}>Older plan did not record a judge choice. Resume defaults approved judge jobs to off. Review this setting and run the selected checks once before resuming.</p>}
          </section>
          <section className="border p-5" style={panel}>
            <h3 className="font-semibold">Review and start</h3>
            <div className="text-sm mt-3 space-y-1" style={{ color: color.sub }}>
              <p>Sources: {cyclePlan.claudeRoot || cyclePlan.codexRoot ? [cyclePlan.claudeRoot && `Claude Code: ${cyclePlan.claudeRoot}`, cyclePlan.codexRoot && `Codex: ${cyclePlan.codexRoot}`].filter(Boolean).join(" · ") : needsCapture ? "No source selected" : "Already captured activity only"}</p>
              <p>Checks: {needsCapture ? "No captured activity to check" : `deterministic run analysis${cyclePlan.runMonitor ? " and the active model-call monitor" : ""}`} · every {cyclePlan.intervalHours} hours after saving</p>
              <p>Approved judge jobs: {cyclePlan.includeApprovedEvaluations ? "one pending approved job may run per cycle, with provider egress and cost" : "off; pending approvals are preserved"}</p>
              <p>Delivery: in-app issue inbox{current("alert", "default")?.state === "active" ? alert.destination === "webhook" ? " · configured webhook for aggregate findings" : " · local log for aggregate findings" : " · no external destination configured"}</p>
            </div>
            <div className="flex flex-wrap gap-2 mt-4"><Button busy={busy === "run"} disabled={!token || needsCapture || (!cyclePlan.analyzeOnly && !cyclePlan.claudeRoot && !cyclePlan.codexRoot)} onClick={async () => { setTestedPlan(null); const result = await post("/api/control/actions/run-schedule", cyclePlan, "run", { reload: false }); if (result) { setLastRun(result); setLastRunPlanKey(planKey); if (cycleVerifiedForPlan(result, cyclePlan)) setTestedPlan(planKey); } }}><ArrowRight size={13} />Run once and review</Button><Button disabled={testedPlan !== planKey} busy={busy === "schedule:daily"} onClick={() => save("schedule", "daily", "active", cyclePlan)}>{schedulePaused ? "Resume recurring checks" : "Save recurring schedule"}</Button>{scheduleSaved && <Button busy={busy === "schedule:daily"} onClick={() => save("schedule", "daily", "disabled", savedSchedule.payload)}>Pause future checks</Button>}</div>
            {scheduleSaved && <p className="text-xs mt-2" style={{ color: color.sub }}>Pausing stops future worker cycles. A check already selected by the worker may finish.</p>}
            {lastRun && <div role="status" className="text-sm mt-3" style={{ color: testedPlan === planKey ? color.green : color.amber }}>{lastRunPlanKey !== planKey ? "Plan changed since the last check. Run this version before saving." : testedPlan === planKey ? "Cycle checks completed." : "The selected checks did not complete. Review the result and retry before saving."} Analysis: {lastRun.analysis?.status || "no result"}. {cyclePlan.runMonitor ? `Model-call monitor: ${lastRun.monitor?.state || lastRun.monitor?.status || "no result"}.` : "No model-call monitor selected."} {lastRun.newIssues > 0 ? `${lastRun.newIssues} new recorded issue${lastRun.newIssues === 1 ? "" : "s"} entered the inbox.` : "No post-activation issue notices were added."}<button type="button" className="underline block mt-2" onClick={() => onNavigate?.({ tab: "monitor", section: "status" })}>View monitoring results →</button></div>}
          </section>
        </div>
        <aside className="space-y-5">
          <section className="border p-5" style={panel}><h3 className="font-semibold">Before you start</h3><p className="text-sm mt-3" style={{ color: color.sub }}>{cyclePlan.includeApprovedEvaluations ? "This plan may consume a separately approved pending judge job, with bounded provider egress and cost." : "This plan runs local checks without consuming pending approved judge jobs or sending session text to a model."}</p><p className="text-sm mt-3" style={{ color: color.sub }}>An external alert destination is optional and configured separately.</p></section>
          <section className="border p-5" style={panel}><div className="flex justify-between gap-2"><h3 className="font-semibold">Worker readiness</h3><button type="button" onClick={load} aria-label="Refresh worker status"><RefreshCw size={14} /></button></div>
            <p role="status" className="text-sm mt-3" style={{ color: currentWorkerCheck && workerRecent ? color.green : color.amber }}>{schedulePaused ? workerRecent ? "Plan paused; worker waiting" : "Plan paused; worker not verified" : scheduleSaved ? workerRecent ? currentWorkerCheck ? "Recurring checks ran on current plan" : "Worker seen; current plan check pending" : "Schedule saved; worker not verified" : "No recurring schedule saved"}</p>
            {data.worker?.lastSeenAt && <p className="text-xs mt-2" style={{ color: color.sub }}>Last worker heartbeat: {workerTime(data.worker.lastSeenAt)}</p>}
            {data.lastCycle?.state === "failed" && <p role="alert" className="text-sm mt-3" style={{ color: color.red }}>Latest {data.lastCycle.trigger} check failed: {String(data.lastCycle.failureCode || "cycle error").replaceAll("_", " ")}. Review the evidence and rerun.</p>}
            {data.lastCycle?.state === "started" && <p role="status" className="text-sm mt-3" style={{ color: color.amber }}>A {data.lastCycle.trigger} check started at {workerTime(data.lastCycle.startedAt)}; completion is not recorded.</p>}
            {data.lastCycle?.state === "completed" && <p className="text-sm mt-3" style={{ color: color.sub }}>Latest completed check: {workerTime(data.lastCycle.finishedAt)} · {data.lastCycle.trigger}{data.lastCycle.trigger === "worker" && savedSchedule?.revision !== data.lastCycle.scheduleRevision ? " · before current plan revision" : ""}.</p>}
            {nextCheck && <p className="text-sm mt-2" style={{ color: color.sub }}>Next worker check eligible after {workerTime(nextCheck)} if this worker keeps running. Refresh afterward to confirm its result.</p>}
            {data.lastCycle?.lastSuccessAt && data.lastCycle.state !== "completed" && <p className="text-xs mt-2" style={{ color: color.sub }}>Earlier successful check: {workerTime(data.lastCycle.lastSuccessAt)}. It does not clear the latest failure or unrecorded outcome.</p>}
            {scheduleSaved && !workerRecent && <div className="border p-3 mt-4 text-sm" style={{ borderColor: color.amber }} role="status">
              <strong>Start the worker for this saved plan</strong>
              <p className="mt-2" style={{ color: color.sub }}>Configure a supervised process with the same store and tenant ID as the dashboard, then run:</p>
              <pre className="overflow-x-auto mt-2 p-2 text-xs" style={{ background: "#0b0e0d" }}>{'verdict-service --tenant-id "$VERDICT_TENANT_ID"'}</pre>
              <p className="mt-2" style={{ color: color.sub }}>Keep it running, refresh this status, and confirm both a recent heartbeat and a completed cycle for this plan.</p>
            </div>}
            {!scheduleSaved && <p className="text-sm mt-3" style={{ color: color.sub }}>A saved schedule does not start the separate <code>verdict-service</code> worker. The worker must use this store and tenant ID.</p>}
            {scheduleSaved && workerRecent && <p className="text-sm mt-3" style={{ color: color.sub }}>The worker is running and polls for plan changes while paused. A heartbeat alone does not confirm that this plan completed a cycle.</p>}
          </section>
        </aside>
      </div>}
    </>}

    {["all", "alerts", "privacy"].includes(section) && <div className="grid lg:grid-cols-2 gap-5">
      {["all", "alerts"].includes(section) && <section className="border p-5" style={panel}><Heading icon={Signal} title="Alert destinations" />
        <Field label="Destination"><select value={alert.destination} onChange={(event) => setAlert({ ...alert, destination: event.target.value })}><option value="local_log">Local finding log</option><option value="webhook">Webhook from environment variable</option></select></Field>
        {alert.destination === "webhook" && <Field label="Webhook URL environment variable"><input value={alert.webhookUrlEnvVar || ""} onChange={(event) => setAlert({ ...alert, webhookUrlEnvVar: event.target.value })} placeholder="VERDICT_ALERT_WEBHOOK_URL" /></Field>}
        <Button busy={busy === "alert:default"} onClick={() => save("alert", "default", "active", alert)}>Save destination</Button>
        <div className="mt-4 space-y-2">{(data?.notifications || []).map((item, index) => <div key={index} className="border p-3 text-sm" style={{ borderColor: color.amber }}>{item.kind}: {item.message}</div>)}{!data?.notifications?.length && <Small>No active finding notifications.</Small>}</div>
        <div className="mt-4" aria-label="Alert delivery queue">
          <div className="flex items-center justify-between gap-2"><div className="text-xs font-mono" style={{ color: color.sub }}>DELIVERY STATUS</div><button type="button" className="underline text-xs" style={{ color: color.green }} onClick={load}>Refresh delivery status</button></div>
          <p className="text-sm mt-2" style={{ color: color.sub }}>
            {Object.entries(data?.deliveryQueueCounts || {}).filter(([, count]) => count > 0).map(([status, count]) => `${count} ${deliveryLabel[status] || status}`).join(" · ") || "No queued alerts."}
            {data?.worker?.status !== "recent" && " · Worker not recently seen; queued alerts will wait."}
          </p>
          <div className="mt-2 space-y-2">{(showAllDeliveryProblems ? deliveryProblems : deliveryProblems.slice(0, 5)).map(deliveryRow)}</div>
          {deliveryProblems.length > 5 && !showAllDeliveryProblems && <button type="button" className="underline mt-3 text-sm" style={{ color: color.green }} onClick={() => setShowAllDeliveryProblems(true)}>Show {deliveryProblems.length - 5} more delivery problems</button>}
          {waitingCount > 0 && <div className="mt-3">
            <button type="button" className="underline text-sm" style={{ color: color.green }} onClick={() => setShowQueuedAlerts((shown) => !shown)}>{showQueuedAlerts ? "Hide" : "Show"} {waitingCount} alerts waiting for the worker or destination</button>
            {showQueuedAlerts && <div className="mt-2 space-y-2">{deliveryWaiting.map(deliveryRow)}</div>}
          </div>}
          {(deliveryHasMore ?? deliveryRows.length < actionableDeliveryCount) && <button type="button" className="underline mt-3 text-sm" style={{ color: color.green }} disabled={!!busy} onClick={async () => { setShowQueuedAlerts(true); setShowAllDeliveryProblems(true); await loadMoreDeliveries(); }}>Show more alerts</button>}
          {rebindPreview && <div className="border p-3 mt-3 text-xs" style={{ borderColor: color.amber }} role="region" aria-label="Review alert destination change">
            <strong>Review this alert before sending it elsewhere</strong>
            <p className="mt-2" style={{ color: color.sub }}>Notification {rebindPreview.notificationId.slice(0, 12)}… from {rebindPreview.oldIntent.sourceKind}. Old revision {rebindPreview.oldIntent.alertRevision}; current revision {rebindPreview.currentAlertRevision}. New destination: {rebindPreview.newDestination}. Fingerprint: {rebindPreview.newDestinationFingerprint?.slice(0, 12) || "URL unavailable"}…</p>
            <div className="flex gap-3 mt-3"><button type="button" className="underline" style={{ color: color.green }} disabled={!!busy} onClick={confirmDeliveryDestination}>Confirm and queue for worker</button><button type="button" className="underline" onClick={() => setRebindPreview(null)}>Cancel</button></div>
          </div>}
        </div>
        <div className="mt-4"><div className="text-xs font-mono" style={{ color: color.sub }}>DELIVERY HISTORY</div><div className="mt-2 space-y-2">{(data?.deliveryAttempts || []).slice(0, 20).map((attempt) => <div key={attempt.attemptId} className="border p-3 text-xs" style={{ borderColor: attempt.outcome === "failed" ? color.red : color.border }}><span style={{ color: attempt.outcome === "failed" ? color.red : color.green }}>{attempt.outcome}</span> · {attempt.sourceKind} · {attempt.attemptedAt}{attempt.httpStatus ? ` · HTTP ${attempt.httpStatus}` : ""}{attempt.errorCode ? ` · ${attempt.errorCode}` : ""}</div>)}{!data?.deliveryAttempts?.length && <Small>No delivery attempts recorded.</Small>}</div></div>
      </section>}
      {["all", "privacy"].includes(section) && <section className="border p-5" style={panel}><Heading icon={Shield} title="Privacy and secrets" />
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={settings.captureContent} disabled />Bounded redacted content capture is on by default</label>
        <Field label="Provider key environment-variable names"><input value={(settings.providerKeyEnvVars || []).join(",")} onChange={(event) => setSettings({ ...settings, providerKeyEnvVars: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} /></Field>
        <Small>Verdict stores names and availability, never key values. For Agent Runs, manage stored evidence retention outside Verdict; this screen does not schedule deletion.</Small>
        <Button busy={busy === "settings:default"} onClick={() => save("settings", "default", "active", settings)}>Save settings</Button>
      </section>}
    </div>}

    {["all", "review"].includes(section) && <section className="border p-5" style={panel}><Heading icon={CheckCircle2} title="Review queue" />
      <div className="mt-4 space-y-2">{openReviews.slice(0, 20).map((item) => <div key={reviewId(item)} className="border p-3 flex flex-wrap items-center gap-3 text-sm" style={{ borderColor: color.border }}><span className="font-mono">{item.traceId}</span><span>{item.dimension}: {item.verdict}</span><button className="ml-auto border px-3 py-1" onClick={() => save("review", reviewId(item), "resolved", { traceId: item.traceId, judgmentId: item.judgmentId, dimension: item.dimension, label: item.verdict })}>Confirm label</button></div>)}{!openReviews.length && <Small>No unresolved fail/unclear judge suggestions in the bounded queue.</Small>}</div>
    </section>}

    {["all", "review"].includes(section) && <section className="border p-5" style={panel}><Heading icon={CheckCircle2} title="Change-request decision log" />
      <Small>These records document a human decision; approving one does not deploy a policy, taxonomy, evaluator, cluster version, or baseline. Use the typed workflow linked from each record to make an actual product change.</Small>
      <div className="grid sm:grid-cols-3 gap-3"><Field label="Category"><select value={proposal.category} onChange={(event) => setProposal({ ...proposal, category: event.target.value })}>{["policy", "taxonomy", "evaluator", "baseline"].map((value) => <option key={value}>{value}</option>)}</select></Field><Field label="Title"><input value={proposal.title} onChange={(event) => setProposal({ ...proposal, title: event.target.value })} /></Field><Field label="Why"><input value={proposal.summary} onChange={(event) => setProposal({ ...proposal, summary: event.target.value })} /></Field></div>
      <Button disabled={!proposal.title || !proposal.summary} busy={busy?.startsWith("proposal:")} onClick={() => { const id = `change-${Date.now()}`; save("proposal", id, "pending", proposal).then(() => setProposal({ category: "policy", title: "", summary: "" })); }}>Create proposal</Button>
      <div className="mt-4 space-y-2">{proposals.map((item) => <div key={item.documentId} className="border p-3 text-sm" style={{ borderColor: color.border }}><div className="font-semibold">{item.payload.title}</div><div className="mt-1" style={{ color: color.sub }}>{item.payload.category} · decision {item.state} · revision {item.revision}</div><div className="mt-1">{item.payload.summary}</div><div className="flex flex-wrap gap-2 mt-3">{item.state === "pending" && <><Button onClick={() => save("proposal", item.documentId, "approved", item.payload)}>Record approval</Button><Button onClick={() => save("proposal", item.documentId, "rejected", item.payload)}>Record rejection</Button></>}{item.revision > 1 && <Button onClick={() => post(`/api/control/proposal/${item.documentId}/rollback`, { targetRevision: item.revision - 1, expectedRevision: item.revision }, `rollback:${item.documentId}`)}><RefreshCw size={13} />Revert decision record</Button>}<Button onClick={() => onNavigate?.(workflowFor(item.payload.category))}>Open typed workflow</Button></div></div>)}</div>
    </section>}
  </div>;
}

function reviewId(item) { return `${item.judgmentId}:${item.dimension}`.replace(/[^A-Za-z0-9._:-]/g, "-").slice(0, 128); }
function workflowFor(category) {
  if (category === "evaluator") return { tab: "evaluate", section: "lab" };
  if (category === "taxonomy") return { tab: "monitor", section: "segments" };
  return { tab: "monitor", section: category === "baseline" ? "history" : "status" };
}
function Heading({ icon: Icon, title }) { return <h2 className="font-semibold flex items-center gap-2 mb-4"><Icon size={15} style={{ color: color.green }} />{title}</h2>; }
function Field({ label, children }) { return <label className="block text-sm mb-3">{label}{React.cloneElement(children, { className: "block w-full mt-1 border p-2 bg-transparent", style: { borderColor: color.border } })}</label>; }
function Button({ children, onClick, busy, disabled }) { return <button disabled={busy || disabled} onClick={onClick} className="inline-flex items-center gap-2 border px-3 py-2 text-sm" style={{ borderColor: color.green, color: disabled ? color.sub : color.green }}>{busy ? "Working…" : children}</button>; }
function Small({ children }) { return <p className="text-xs my-3" style={{ color: color.sub }}>{children}</p>; }
