import React, { useEffect, useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2 } from "lucide-react";
import { Insights } from "./Insights.jsx";
import { Usage } from "./Usage.jsx";
import { analysisNeedsRefresh, isEvidenceGap, monitoringStatus, nextAction } from "./guided-flow.mjs";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };

function Panel({ children, className = "" }) {
  return <section className={`border p-5 ${className}`} style={{ background: C.panel, borderColor: C.border }}>{children}</section>;
}

function utcStamp(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric",
    year: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

export function GuidedHome({ meta, monitor, managementReport, insightsUrl, usageUrl, configUrl, source,
  onOpenRun, onOpenFinding, onNavigate, onReload }) {
  const [insights, setInsights] = useState(null);
  const [insightsError, setInsightsError] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [readinessError, setReadinessError] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [usageSnapshot, setUsageSnapshot] = useState(null);
  const receiveUsage = React.useCallback((url, data) => setUsageSnapshot({ url, data }), []);
  const receiveInsights = React.useCallback((data, error) => {
    setInsights(data);
    setInsightsError(error);
  }, []);
  const root = configUrl.replace(/\/api\/config$/, "");
  useEffect(() => {
    if (source !== "live") return undefined;
    let active = true;
    fetch(`${root}/api/readiness`, { credentials: "same-origin" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then((data) => { if (active) { setReadiness(data); setReadinessError(false); } })
      .catch(() => { if (active) setReadinessError(true); });
    return () => { active = false; };
  }, [root, source, meta, retryKey]);

  const action = source === "new" ? nextAction({ meta })
    : source === "live" && (readinessError || insightsError)
      ? { id: "retry", title: "Some evidence could not be loaded",
          description: "Retry the local dashboard requests before relying on a monitoring suggestion." }
      : source === "live" && readiness
        ? nextAction({ meta, insights, monitor, control: readiness }) : null;
  const watch = source === "live" && readiness ? monitoringStatus(monitor, readiness) : null;
  const runs = meta.totalAgentRuns || 0;
  const calls = meta.monitorEligibleCalls || 0;
  const sourceNames = (meta.agentRunSources || []).map((item) => item.sourceKind);
  const sourceSummary = sourceNames.length ? [...new Set(sourceNames)].join(" · ") : calls ? "Completed provider calls" : "No captured agent source";
  const staleAnalysis = analysisNeedsRefresh(meta, insights);
  const latestRun = meta.lastAgentCaptureAt ? utcStamp(meta.lastAgentCaptureAt) : null;
  const latestCall = meta.runEnd ? utcStamp(meta.runEnd) : null;
  const usageData = usageSnapshot && usageSnapshot.url === usageUrl ? usageSnapshot.data : null;
  const daysWithTurnTotals = usageData?.days?.filter((day) => day.tokenKnownTurns > 0 && Number.isFinite(day.totalTokens)) || [];
  const callDaysWithTotals = managementReport?.timeline?.rows?.filter((day) => day.tokenKnownCalls > 0 && Number.isFinite(day.totalTokens)) || [];
  const issueOnlyDays = !daysWithTurnTotals.length && !callDaysWithTotals.length
    && usageData?.days?.some((day) => day.recordedIssueEvents > 0);
  const tokenMetric = daysWithTurnTotals.length
    ? { value: daysWithTurnTotals.reduce((sum, day) => sum + day.totalTokens, 0).toLocaleString(), label: "Reported turn tokens", detail: "Last 30 UTC days · source reported" }
    : callDaysWithTotals.length
      ? { value: callDaysWithTotals.reduce((sum, day) => sum + day.totalTokens, 0).toLocaleString(), label: "Known model-call tokens", detail: "Selected report period · not assigned to sessions" }
      : { value: "—", label: "Reported tokens", detail: "No complete token totals captured" };
  const currentIssues = insights?.analysisState?.status === "completed" && !staleAnalysis
    ? (insights.findings || []).filter((finding) => !isEvidenceGap(finding)).length : null;
  const evidenceGapTypes = currentIssues === 0
    ? (insights.findings || []).filter(isEvidenceGap).length : 0;
  const openNotices = Number.isInteger(readiness?.openIssues) ? readiness.openIssues : null;
  const hasRecurringSchedule = readiness?.documents?.some((item) => item.kind === "schedule"
    && item.documentId === "daily" && ["active", "disabled"].includes(item.state));
  const showNotices = openNotices != null && (hasRecurringSchedule || openNotices > 0);
  const leadWithNotices = openNotices > 0 || (currentIssues == null && showNotices);
  const leadWithGaps = !leadWithNotices && evidenceGapTypes > 0;
  const actionLabel = action?.id === "retry" ? "Retry loading"
    : action?.id === "review_issues" ? "Open recorded issues" : action?.id === "analyze" ? "Go to local analysis"
      : action?.id === "review_findings" ? "Inspect recorded findings"
      : action?.id === "inspect_gaps" ? "Inspect evidence gaps"
      : action?.id === "capture" ? "Set up capture" : ["plan", "schedule", "saved", "resume", "run_cycle"].includes(action?.id)
        ? "Review suggested setup" : "Open next step";
  const clickAction = () => {
    if (!action) return;
    if (action.id === "retry") { setReadiness(null); setReadinessError(false); setInsightsError(null); setRetryKey((value) => value + 1); }
    else if (action.id === "review_issues") onNavigate("monitor", "status");
    else if (action.id === "monitor_alert") onNavigate("monitor", "status");
    else if (action.id === "capture") onNavigate("settings", "sources");
    else if (action.id === "monitor") onNavigate("monitor", "history");
    else if (["plan", "schedule", "run_cycle", "saved", "resume", "check_failed", "check_unfinished"].includes(action.id)) onNavigate("monitor", "plan");
    else if (["analyze", "inspect_gaps", "review_findings"].includes(action.id)) {
      const findings = document.getElementById(action.id === "inspect_gaps" ? "guided-evidence-gaps" : "guided-findings");
      findings?.scrollIntoView({ behavior: "smooth", block: "start" });
      findings?.focus({ preventScroll: true });
    }
    else onNavigate("explore", "runs");
  };

  return <div className="space-y-5 verdict-home">
    {readinessError && <div role="status" className="text-sm flex gap-2" style={{ color: C.amber }}><AlertTriangle size={15} />Setup status could not be loaded; no recurring check is assumed.</div>}
    {(source === "live" || source === "new") && <section className="verdict-home-hero">
      <div className="verdict-home-stamp">{action?.id === "plan" ? "SETUP RECOMMENDATION" : "NEXT STEP"}</div>
      {action ? <><h3 className="text-lg font-semibold mt-1">{action.title}</h3><p className="text-sm mt-2" style={{ color: C.sub }}>{action.description}</p>
        <button type="button" onClick={clickAction} className="verdict-home-primary-action">{actionLabel}<ArrowRight size={14} /></button></>
        : <p role="status" className="text-sm mt-2" style={{ color: C.sub }}>Checking captured evidence and setup…</p>}
    </section>}
    {source === "loading" ? <Panel><p role="status" className="text-sm" style={{ color: C.sub }}>Loading captured activity and monitoring status…</p></Panel> : <div className="grid sm:grid-cols-3 gap-3">
      <section className="verdict-home-metric"><div>Captured agent runs</div><strong>{runs.toLocaleString()}</strong><span>{sourceSummary}</span></section>
      <section className="verdict-home-metric"><div>{tokenMetric.label}</div><strong>{tokenMetric.value}</strong><span>{tokenMetric.detail}</span></section>
      <section className="verdict-home-metric"><div>{leadWithNotices ? "Open recurring issues" : leadWithGaps ? "Evidence gaps" : runs || calls ? "Recorded issue types" : "Issues to inspect"}</div><strong>{leadWithNotices ? openNotices.toLocaleString() : leadWithGaps ? evidenceGapTypes.toLocaleString() : currentIssues == null ? "—" : currentIssues.toLocaleString()}</strong><span>{leadWithNotices ? "Recorded failed events or terminal run statuses from completed checks" : !runs && !calls ? "Capture activity to look for issues" : currentIssues == null ? "Run local analysis or refresh saved findings" : leadWithGaps ? "Current analyzed scope · inspect below" : showNotices ? "Current analyzed scope · 0 recurring notices" : "Current analyzed scope"}</span></section>
    </div>}
    {(runs > 0 || calls > 0) && <div className="verdict-home-evidence-grid">
      <div><div className="verdict-home-section-title"><h2>{issueOnlyDays ? "Recorded issues by day" : "Usage over time"}</h2><p>{issueOnlyDays ? "Choose a day to inspect recorded issue events." : "Choose a day to inspect the captured token evidence."}</p></div>
      {source === "live" && (runs > 0 || calls > 0)
        ? <Usage compact home url={usageUrl} onOpenRun={onOpenRun} modelCallReport={managementReport}
          onOpenCalls={(date) => onNavigate("explore", "calls", date)} onData={receiveUsage} />
        : <Panel><h3 className="font-semibold">Token trend needs captured activity</h3><p className="text-sm mt-2" style={{ color: C.sub }}>Capture agent runs or model calls to inspect usage by day.</p><button type="button" className="mt-3 text-sm underline" style={{ color: C.green }} onClick={() => onNavigate("settings", "sources")}>Set up agent capture</button></Panel>}</div>
      <div id="guided-findings" tabIndex={-1}><div className="verdict-home-section-title"><h2>What to look at</h2><p>Recorded findings and evidence gaps linked to affected runs.</p></div><Insights key={retryKey} mode="home" url={insightsUrl} currentMeta={meta} onData={receiveInsights} onOpenRuns={onOpenFinding} onAnalysisComplete={onReload} /></div>
    </div>}
    {watch && <section className="verdict-home-watch" aria-label="Monitoring status"><div><strong>{watch.title}</strong><p>{watch.description}</p></div><button type="button" onClick={() => onNavigate("monitor", "status")}>View monitoring →</button></section>}
    {(latestRun || latestCall) && <p className="text-xs" style={{ color: C.sub }}>
      {latestRun && <>Latest agent run started: {latestRun}</>}
      {latestRun && latestCall && " · "}
      {latestCall && <>Latest model call started: {latestCall}</>}
      {" UTC"}
    </p>}
    {insights?.analysisState?.status === "completed" && !staleAnalysis && !insights.findings?.length && <div className="text-sm flex gap-2" style={{ color: C.sub }}><CheckCircle2 size={16} style={{ color: C.green }} />No recorded issue in the analyzed scope. This does not assess uncaptured work.</div>}
  </div>;
}
