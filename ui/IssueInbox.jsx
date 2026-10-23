import React, { useEffect, useRef, useState } from "react";
import { analysisNeedsRefresh, expectedNextCheck, monitoringStatus, primaryFinding } from "./guided-flow.mjs";

const COLORS = { border: "#26332e", panel: "#111715", sub: "#94a39d", green: "#4ee1aa", amber: "#f2b84b" };
const LABELS = {
  tool_error: "A tool reported an error",
  command_failed: "A command failed",
  test_failed: "A test failed",
  business_outcome_failed: "A business outcome failed",
  run_failed: "An agent run ended failed",
  run_timed_out: "An agent run timed out",
};
const RECHECK = {
  tool_error: "Repeat the relevant tool operation and inspect its later result.",
  command_failed: "Repeat the relevant command and inspect its exit status.",
  test_failed: "Rerun the relevant test and inspect its later result.",
  business_outcome_failed: "Repeat or verify the relevant business outcome with a recorded result.",
  run_failed: "Inspect the run's recorded execution status and events.",
  run_timed_out: "Inspect where the run stopped and its recorded timeout.",
};
const issueNextCheck = (issue) => {
  const count = Array.isArray(issue.rechecks) ? issue.rechecks.length : 0;
  if (count > 1) return "Compare the failed event with the attached later passes and verify which, if any, cover the same work.";
  if (count === 1) return "Compare the failed event with the attached later pass and verify it covers the same work.";
  return RECHECK[issue.checkCode] || "Repeat the relevant check and inspect its result.";
};
const ANALYSIS_LABELS = {
  tool_error: "Tool failures recorded", command_failed: "Commands failed",
  test_failed: "Test failures recorded", business_outcome_failed: "Reported business outcomes failed",
  possible_tool_loop: "Repeated identical tool calls", required_step_missing: "Required evidence missing",
  prohibited_tool_used: "Prohibited tool used", response_schema_invalid: "Responses failed the JSON requirement",
};
const displayTime = (value) => {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : "time unavailable";
};

function RecheckEvidence({ issue, token, root, onOpenEvidence, onRefresh }) {
  const [runId, setRunId] = useState("");
  const [eventId, setEventId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState(null);
  const searchGeneration = useRef(0);
  const attachDetails = useRef(null);
  const runInput = useRef(null);
  const rechecks = Array.isArray(issue.rechecks) ? issue.rechecks : [];
  const currentSearch = search?.noticeId === issue.noticeId ? search : null;
  const canSearch = ["command_failed", "test_failed"].includes(issue.checkCode);

  async function findCandidates() {
    if (!canSearch || currentSearch?.loading) return;
    const generation = ++searchGeneration.current;
    const noticeId = issue.noticeId;
    setSearch({ noticeId, loading: true, data: null, error: null });
    try {
      const response = await fetch(`${root}/api/control/issues/${encodeURIComponent(noticeId)}/candidate-rechecks`, {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Could not search later recorded checks.");
      const data = await response.json();
      if (generation === searchGeneration.current) setSearch({ noticeId, loading: false, data, error: null });
    } catch {
      if (generation === searchGeneration.current) setSearch({ noticeId, loading: false, data: null,
        error: "Could not search later recorded checks." });
    }
  }

  async function attach(event) {
    event.preventDefault();
    if (!token || busy || !runId.trim() || !eventId.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${root}/api/control/issues/${encodeURIComponent(issue.noticeId)}/rechecks`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: JSON.stringify({ runId: runId.trim(), eventId: eventId.trim() }),
      });
      if (!response.ok) throw new Error("The selected event was not a later explicit pass for this issue.");
      setRunId("");
      setEventId("");
      setSearch(null);
      await onRefresh();
    } catch (failure) {
      setError(failure.message || "Could not attach the recorded recheck.");
    } finally {
      setBusy(false);
    }
  }

  return <div className="text-sm mt-3 space-y-2">
    {rechecks.map((item) => <div key={`${item.runId}:${item.eventId}`} className="border-l pl-3" style={{ borderColor: COLORS.green }}>
      <p>{item.sameCheckId === true
        ? "Later pass with a matching application-supplied check ID recorded"
        : "Later recorded pass attached"} {displayTime(item.eventAt)}; {item.sameCheckId === true
        ? "confirm the inputs and task; recovery remains unverified."
        : "the same work and overall recovery remain unverified."}</p>
      <button type="button" className="underline" style={{ color: COLORS.green }}
        onClick={() => onOpenEvidence?.({ runId: item.runId, eventId: item.eventId,
          noticeId: issue.noticeId,
          ...(item.recentRevision ? { recentRevision: item.recentRevision,
            recentTurnId: item.recentTurnId } : {}) })}>Open recheck evidence →</button>
    </div>)}
    {canSearch && <div className="border-l pl-3 space-y-2" style={{ borderColor: COLORS.border }}>
      <button type="button" className="underline" style={{ color: COLORS.green }}
        disabled={currentSearch?.loading} onClick={findCandidates}>Find later matching checks</button>
      {currentSearch?.loading && <p style={{ color: COLORS.sub }}>Searching recorded events…</p>}
      {currentSearch?.error && <p role="alert" style={{ color: COLORS.amber }}>{currentSearch.error}</p>}
      {currentSearch?.data && <>
        {!currentSearch.data.searchable
          ? <p style={{ color: COLORS.sub }}>{currentSearch.data.partial
            ? "The failed event is too large for this bounded search. Inspect and attach a later event manually."
            : "No valid check ID was recorded for this failure. Inspect and attach a later event manually."}</p>
          : <>
            <p style={{ color: COLORS.sub }}>Searched the first {currentSearch.data.scanned} later event records for the same recorded check ID.
              {currentSearch.data.partial ? " More may exist; inspect or attach a known later event manually." : " A matching ID does not prove the same inputs, task, or repair."}</p>
            {currentSearch.data.candidates.length === 0 && <p style={{ color: COLORS.sub }}>No matching recorded pass appeared in this scan. This does not prove the issue persisted.</p>}
            {currentSearch.data.candidates.map((candidate) => <div key={`${candidate.runId}:${candidate.eventId}`}>
              <span>Later recorded pass · {displayTime(candidate.eventAt)} </span>
              <button type="button" className="underline mr-2" style={{ color: COLORS.green }}
                onClick={() => onOpenEvidence?.({ runId: candidate.runId, eventId: candidate.eventId,
                  ...(candidate.recentRevision ? { recentRevision: candidate.recentRevision,
                    recentTurnId: candidate.recentTurnId } : {}) })}>Inspect candidate →</button>
              <button type="button" className="underline" style={{ color: COLORS.green }}
                onClick={() => {
                  setRunId(candidate.runId);
                  setEventId(candidate.eventId);
                  if (attachDetails.current) attachDetails.current.open = true;
                  runInput.current?.focus();
                }}>Use these IDs</button>
            </div>)}
          </>}
      </>}
    </div>}
    <details ref={attachDetails}><summary className="cursor-pointer" style={{ color: COLORS.green }}>Attach a later recorded pass</summary>
      <form onSubmit={attach} className="mt-2 flex flex-wrap gap-2 items-end">
        <label>Later run ID<input ref={runInput} className="block border px-2 py-1 bg-transparent" style={{ borderColor: COLORS.border }}
          value={runId} onChange={(event) => setRunId(event.target.value)} /></label>
        <label>Later event ID<input className="block border px-2 py-1 bg-transparent" style={{ borderColor: COLORS.border }}
          value={eventId} onChange={(event) => setEventId(event.target.value)} /></label>
        <button type="submit" disabled={!token || busy || !runId.trim() || !eventId.trim()}
          className="border px-2 py-1" style={{ borderColor: COLORS.green, color: COLORS.green }}>Attach pass</button>
      </form>
      <p style={{ color: COLORS.sub }}>Choose a later event from Investigation. Verdict checks its recorded result; the link does not verify a repair.</p>
      {error && <p role="alert" style={{ color: COLORS.amber }}>{error}</p>}
    </details>
  </div>;
}

export function IssueInbox({ configUrl, insightsUrl = null, meta = {}, monitor = null,
  onOpenEvidence, onOpenFinding, onOpenPlan,
  onOpenUsage, onOpenHistory, onOpenGroups, onOpenDeliveries,
  onRefreshStore = null }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const [page, setPage] = useState({ issues: [], nextOffset: null, state: null });
  const [token, setToken] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [showReviewed, setShowReviewed] = useState(false);
  const [analysis, setAnalysis] = useState({ loading: Boolean(insightsUrl), data: null, error: false });
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const requestEpoch = useRef(0);
  const analysisEpoch = useRef(0);
  const sourceEpoch = useRef(0);
  const selectedFilter = useRef("open");

  async function loadAnalysis() {
    if (!insightsUrl) return;
    const epoch = ++analysisEpoch.current;
    setAnalysis({ loading: true, data: null, error: false });
    try {
      const response = await fetch(insightsUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error("saved analysis unavailable");
      const data = await response.json();
      if (epoch === analysisEpoch.current) setAnalysis({ loading: false, data, error: false });
    } catch {
      if (epoch === analysisEpoch.current) setAnalysis({ loading: false, data: null, error: true });
    }
  }
  async function runAnalysis() {
    if (!token || analysisBusy) return;
    const sourceAtStart = sourceEpoch.current;
    setAnalysisBusy(true);
    setAnalysis({ loading: true, data: null, error: false });
    try {
      const response = await fetch(`${root}/api/insights/run`, {
        method: "POST", credentials: "same-origin",
        headers: { "X-Verdict-Setup": token, Accept: "application/json" },
      });
      if (!response.ok) throw new Error("local analysis failed");
      await response.json();
      if (sourceAtStart !== sourceEpoch.current) return;
      await loadAnalysis();
      if (sourceAtStart !== sourceEpoch.current) return;
      await onRefreshStore?.();
    } catch {
      if (sourceAtStart === sourceEpoch.current) setAnalysis({ loading: false, data: null, error: true });
    } finally { if (sourceAtStart === sourceEpoch.current) setAnalysisBusy(false); }
  }

  async function load(offset = 0, state = showReviewed ? "reviewed" : "open") {
    const epoch = ++requestEpoch.current;
    setPage((current) => ({ ...current, state: null }));
    setReadiness(null);
    setError(null);
    try {
      const [issuesResponse, tokenResponse, readinessResponse] = await Promise.all([
        fetch(`${root}/api/control/issues?offset=${offset}&state=${state}`, { credentials: "same-origin" }),
        fetch(`${root}/api/setup/token`, { credentials: "same-origin" }),
        fetch(`${root}/api/readiness`, { credentials: "same-origin" }),
      ]);
      if (!issuesResponse.ok || !tokenResponse.ok || !readinessResponse.ok) throw new Error("Issue inbox unavailable");
      const [issues, auth, status] = await Promise.all([issuesResponse.json(), tokenResponse.json(), readinessResponse.json()]);
      if (epoch !== requestEpoch.current) return false;
      if (issues.state !== state) throw new Error("issue filter mismatch");
      setPage((current) => offset ? {
        issues: [...current.issues, ...issues.issues], nextOffset: issues.nextOffset, state,
      } : issues);
      setToken(auth.setupToken);
      setReadiness(status);
      setError(null);
      return true;
    } catch (failure) {
      if (epoch === requestEpoch.current) setError("Could not refresh monitoring status. Retry the request.");
      return false;
    }
  }
  useEffect(() => { load(); return () => { requestEpoch.current += 1; }; }, [root, showReviewed, meta]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void loadAnalysis(); return () => { analysisEpoch.current += 1; }; }, [insightsUrl, meta]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void onRefreshStore?.(); }, [root]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setAnalysisBusy(false);
    setBusy(null);
    return () => { sourceEpoch.current += 1; };
  }, [root, insightsUrl]);

  function selectReviewed(value) {
    selectedFilter.current = value ? "reviewed" : "open";
    setShowReviewed(value);
  }

  async function review(notice, action) {
    const sourceAtStart = sourceEpoch.current;
    setBusy(notice.noticeId);
    try {
      const response = await fetch(`${root}/api/control/issues/${notice.noticeId}/${action}`, {
        method: "POST", credentials: "same-origin", headers: { "X-Verdict-Setup": token },
      });
      if (!response.ok) throw new Error("This issue changed; refresh the inbox");
      await response.json();
      if (sourceAtStart !== sourceEpoch.current) return;
      setPage({ issues: [], nextOffset: null, state: null });
      await load(0, selectedFilter.current);
    } catch (failure) { if (sourceAtStart === sourceEpoch.current) setError(String(failure)); }
    finally { if (sourceAtStart === sourceEpoch.current) setBusy(null); }
  }

  const visible = page.issues;
  if (!readiness || page.state !== (showReviewed ? "reviewed" : "open")) return <section className="border p-5" style={{ background: COLORS.panel, borderColor: COLORS.border }}>
    <h2 className="text-lg font-semibold">Monitoring</h2>
    {error ? <><p role="alert" className="text-sm mt-2" style={{ color: COLORS.amber }}>{error}</p><button type="button" onClick={() => load(0)} className="border px-3 py-2 text-sm mt-3" style={{ borderColor: COLORS.green, color: COLORS.green }}>Retry loading</button></>
      : <p role="status" className="text-sm mt-2" style={{ color: COLORS.sub }}>Loading monitoring status…</p>}
  </section>;
  const totalOpen = Number.isInteger(readiness?.openIssues) ? readiness.openIssues : 0;
  const scheduleActive = readiness?.documents?.some((item) => item.kind === "schedule" && item.state === "active");
  const schedulePaused = readiness?.documents?.some((item) => item.kind === "schedule" && item.state === "disabled");
  const workerRecent = scheduleActive && readiness?.worker?.status === "recent";
  const savedPlan = readiness?.documents?.find((item) => item.kind === "schedule"
    && item.documentId === "daily" && item.state === "active");
  const nextCheck = expectedNextCheck(savedPlan, readiness);
  const status = monitoringStatus(monitor, readiness);
  const currentCheck = workerRecent && readiness?.lastCycle?.trigger === "worker"
    && readiness.lastCycle.scheduleRevision === savedPlan?.revision
    && readiness.lastCycle.state === "completed";
  const activeComparison = monitor?.active?.snapshot;
  const collecting = activeComparison?.manifest?.prospective_open === true;
  const alertCount = collecting ? 0 : (activeComparison?.comparison?.metrics || [])
    .filter((metric) => metric.alert).length;
  const currentCalls = activeComparison?.manifest?.current_unit_ids?.length || 0;
  const targetCalls = monitor?.active?.policy?.prospective_target;
  const snapshot = analysis.data;
  const analysisFailed = snapshot?.analysisState?.status === "error";
  const snapshotStale = snapshot && analysisNeedsRefresh(meta, snapshot);
  const actionable = snapshot?.analysisState?.status === "completed" && !snapshotStale
    ? primaryFinding(snapshot) : null;
  const modelWatch = !monitor?.active
    ? Number(meta.monitorEligibleCalls || 0) ? "Ready for a reviewed comparison" : "Connect completed model calls first"
    : collecting ? `Collecting ${currentCalls} of ${targetCalls || "the target"} eligible calls`
      : alertCount ? `${alertCount} model-call change${alertCount === 1 ? "" : "s"} need review`
        : activeComparison?.comparison?.status === "no_alert" ? "Latest completed comparison: no alert"
          : "Review comparison coverage";
  const leadFinding = actionable && !totalOpen && !alertCount
    && readiness?.lastCycle?.state !== "failed"
    && readiness?.lastCycle?.state !== "started";
  const affectedRunCount = actionable?.runs || actionable?.runIds?.length || 0;
  const affectedRunLabel = `${affectedRunCount} affected run${affectedRunCount === 1 ? "" : "s"}`;
  const leadNotice = !showReviewed && totalOpen ? visible[0] : null;
  const deliveryProblems = ["failed", "blocked", "exhausted"].reduce((total, status) => {
    const value = readiness?.deliveryQueue?.[status];
    return total + (Number.isSafeInteger(value) && value > 0 ? value : 0);
  }, 0);
  const deliveryLabel = `${deliveryProblems} alert deliver${deliveryProblems === 1 ? "y needs" : "ies need"} attention`;
  return <div className="space-y-5">
    {analysisFailed && <section role="alert" className="border p-4 flex flex-wrap items-center justify-between gap-3" style={{ background: COLORS.panel, borderColor: COLORS.amber }}><div><strong>Latest local analysis failed</strong><p className="text-sm mt-1" style={{ color: COLORS.sub }}>Run the local check again before relying on saved findings.</p></div><button type="button" disabled={!token || analysisBusy} onClick={runAnalysis} className="border px-3 py-2 text-sm" style={{ borderColor: COLORS.green, color: COLORS.green }}>Retry local analysis</button></section>}
    {deliveryProblems > 0 && <section role="alert" className="border p-4 flex flex-wrap items-center justify-between gap-3" style={{ background: COLORS.panel, borderColor: COLORS.amber }}><div><strong>{deliveryLabel}</strong><p className="text-sm mt-1" style={{ color: COLORS.sub }}>A configured destination has failed, been blocked, or exhausted retries. Inspect the delivery record before relying on external alerts.</p></div><button type="button" onClick={onOpenDeliveries} className="border px-3 py-2 text-sm" style={{ borderColor: COLORS.green, color: COLORS.green }}>Review alert deliveries →</button></section>}
    {leadNotice && <div><h2 className="text-lg font-semibold">Needs your attention</h2><p className="text-sm mt-1" style={{ color: COLORS.sub }}>Open the evidence, then choose the next action.</p></div>}
    {leadNotice && <section className="border p-5 flex flex-wrap items-center justify-between gap-4" style={{ background: "#1b1a14", borderColor: COLORS.amber }}>
      <div><div className="text-xs font-mono" style={{ color: COLORS.amber }}>RECORDED FAILURE TO INSPECT</div>
        <h3 className="text-lg font-semibold mt-2">{LABELS[leadNotice.checkCode] || leadNotice.checkCode}</h3>
        <p className="text-sm mt-1" style={{ color: COLORS.sub }}>Observed {displayTime(leadNotice.eventAt)}. {issueNextCheck(leadNotice)}</p>
        <p className="text-xs mt-2" style={{ color: COLORS.sub }}>Reviewing this notice does not confirm that a later run recovered.</p></div>
      <div className="flex flex-wrap gap-3 items-center text-sm"><button type="button" onClick={() => onOpenEvidence(leadNotice)} className="border px-3 py-2" style={{ color: COLORS.green, borderColor: COLORS.green }}>Open run evidence →</button>
        <button type="button" disabled={!token || busy === leadNotice.noticeId} onClick={() => review(leadNotice, "acknowledge")} className="underline">Acknowledge</button>
        <button type="button" disabled={!token || busy === leadNotice.noticeId} onClick={() => review(leadNotice, "dismiss")} className="underline">Dismiss</button>
        <button type="button" onClick={() => { void load(); void loadAnalysis(); void onRefreshStore?.(); }} className="underline">Refresh</button></div>
      <details className="w-full text-sm"><summary className="cursor-pointer" style={{ color: COLORS.green }}>Prepare a local repair brief</summary>
        <div className="border-l pl-3 mt-3 space-y-2" style={{ borderColor: COLORS.border }}>
          <p><strong>Observed:</strong> {LABELS[leadNotice.checkCode] || leadNotice.checkCode} at {leadNotice.eventAt}.</p>
          <p><strong>Evidence:</strong> run {leadNotice.runId}{leadNotice.evidenceKind === "run" ? ". Inspect the recorded run status before proposing a fix." : `, event ${leadNotice.eventId}. Inspect the event before proposing a fix.`}</p>
          <p><strong>Cause:</strong> {leadNotice.evidenceKind === "run" ? "not established by the recorded run status alone." : "not established by this event alone."}</p>
          <p><strong>Next check:</strong> {issueNextCheck(leadNotice)}</p>
          <p><strong>Recovery:</strong> unverified. Acknowledge and dismiss only change review state.</p>
        </div>
      </details>
      {leadNotice.evidenceKind !== "run" && <RecheckEvidence key={leadNotice.noticeId} issue={leadNotice} token={token} root={root} onOpenEvidence={onOpenEvidence}
        onRefresh={() => load()} />
      }
    </section>}
    <section className="border p-5 flex flex-wrap justify-between gap-4 items-center" style={{ background: workerRecent ? "#17251e" : COLORS.panel, borderColor: leadFinding ? COLORS.amber : workerRecent ? "#365b47" : COLORS.border }}>
      <div><div className="text-xs font-mono" style={{ color: workerRecent ? COLORS.green : COLORS.amber }}>{leadFinding ? "RECORDED ISSUE TO REVIEW" : status.id === "alert" ? "MODEL-CALL ALERT" : schedulePaused ? "PLAN PAUSED" : status.id === "collecting" ? "MODEL-CALL MONITOR COLLECTING" : currentCheck ? "AGENT CHECKS VERIFIED" : workerRecent ? "WORKER WAITING FOR THIS PLAN" : scheduleActive ? "PLAN SAVED · WORKER NOT VERIFIED" : "NO RECURRING PLAN"}</div>
        <h3 className="font-semibold mt-2">{leadFinding ? ANALYSIS_LABELS[actionable.code] || actionable.code.replaceAll("_", " ") : currentCheck && status.id === "worker_recent" ? "Latest scheduled check completed" : status.title}</h3><p className="text-sm mt-1" style={{ color: COLORS.sub }}>{leadFinding ? `Saved analysis found ${affectedRunLabel} across ${Number(snapshot.scope?.analyzedRuns || 0).toLocaleString()} of ${Number(snapshot.scope?.availableRuns || 0).toLocaleString()} runs. Inspect the recorded events, then decide what to change.` : currentCheck && status.id === "worker_recent" ? "The worker completed a check on this plan. Review recorded issues and the next eligible check below." : status.description}</p>
        {leadFinding && snapshot.analysisState?.completedAt && <p className="text-xs mt-2" style={{ color: COLORS.sub }}>Analysis completed: {displayTime(snapshot.analysisState.completedAt)}{snapshot.analysisState.cutoff ? ` · started: ${displayTime(snapshot.analysisState.cutoff)}` : ""}</p>}</div>
      <button type="button" onClick={leadFinding ? () => onOpenFinding?.(actionable) : onOpenPlan} className="border px-3 py-2 text-sm" style={{ borderColor: COLORS.border, color: COLORS.green }}>{leadFinding ? "Inspect affected runs" : schedulePaused ? "Review and resume" : scheduleActive ? "Change checks" : "Review suggested plan"}</button>
    </section>
    <div className="grid sm:grid-cols-3 gap-3 text-sm" aria-label="Monitoring at a glance">
      <div className="border p-4" style={{ background: COLORS.panel, borderColor: COLORS.border }}><div style={{ color: COLORS.sub }}>Captured agent runs</div><div className="text-xl font-semibold mt-1">{Number(meta.totalAgentRuns || 0).toLocaleString()}</div><div className="text-xs mt-1" style={{ color: COLORS.sub }}>Stored evidence at the last dashboard refresh</div></div>
      <div className="border p-4" style={{ background: COLORS.panel, borderColor: COLORS.border }}><div style={{ color: COLORS.sub }}>Open recurring issues</div><div className="text-xl font-semibold mt-1">{totalOpen.toLocaleString()}</div><div className="text-xs mt-1" style={{ color: COLORS.sub }}>Recorded failed events or terminal run statuses since plan activation</div></div>
      <div className="border p-4" style={{ background: COLORS.panel, borderColor: COLORS.border }}><div style={{ color: COLORS.sub }}>Next local check</div><div className="font-semibold mt-2">{schedulePaused ? "Paused" : scheduleActive && !workerRecent ? "Worker not verified" : nextCheck ? new Date(nextCheck).toLocaleString() : workerRecent ? "Waiting for worker result" : "Not scheduled"}</div><div className="text-xs mt-1" style={{ color: COLORS.sub }}>{nextCheck && workerRecent ? "Earliest eligible time; confirm the completed result afterward" : scheduleActive && !workerRecent ? "Start the worker for this store" : "Review the recurring plan to start checks"}</div></div>
    </div>
    {!leadNotice && (leadFinding || alertCount > 0) && <div><h2 className="text-lg font-semibold">Needs your attention</h2><p className="text-sm mt-1" style={{ color: COLORS.sub }}>Open the evidence, then choose the next action.</p></div>}
    {readiness?.lastCycle?.state === "failed" && <p role="alert" className="border p-3 text-sm" style={{ borderColor: COLORS.amber, color: COLORS.amber }}>Latest {readiness.lastCycle.trigger} check failed: {String(readiness.lastCycle.failureCode || "cycle error").replaceAll("_", " ")}. Review the plan and rerun.</p>}
    {readiness?.lastCycle?.state === "started" && <p role="status" className="border p-3 text-sm" style={{ borderColor: COLORS.amber, color: COLORS.amber }}>A check started {displayTime(readiness.lastCycle.startedAt)}, but completion was not recorded. Check the worker before relying on this watch.</p>}
    {readiness?.lastCycle?.state === "completed" && <p className="text-sm" style={{ color: COLORS.sub }}>Latest completed {readiness.lastCycle.trigger} check: {displayTime(readiness.lastCycle.finishedAt)}. {readiness.lastCycle.trigger === "manual" ? "This does not prove the recurring worker ran." : "Open the plan to review its current revision and worker status."}</p>}
    {!leadFinding && !leadNotice && <div><h3 className="text-lg font-semibold">Needs your attention</h3><p className="text-sm mt-1" style={{ color: COLORS.sub }}>{totalOpen ? "Open a recorded event, check the evidence, then choose the next action." : alertCount ? "Open the model-call comparison to inspect the changed metric and examples." : actionable ? "Inspect the linked runs in the saved analysis, then decide whether to start recurring checks." : analysis.loading ? "Loading saved findings before suggesting a next step." : snapshotStale ? "Refresh the saved analysis before interpreting issue status." : "No open recurring notices or completed comparison alert in this view. Review saved analysis coverage below."}</p></div>}
    {insightsUrl && !leadFinding && <section className="border p-4" style={{ background: COLORS.panel, borderColor: actionable ? COLORS.amber : COLORS.border }}>
      <div className="text-xs font-mono" style={{ color: COLORS.green }}>SAVED DETERMINISTIC ANALYSIS</div>
      {analysis.loading ? <p role="status" className="text-sm mt-2">Loading saved findings…</p>
        : analysis.error ? <><p role="alert" className="text-sm mt-2">Local analysis or saved findings could not be loaded. No updated finding is assumed.</p><button type="button" disabled={!token || analysisBusy} onClick={runAnalysis} className="border px-3 py-2 text-sm mt-2" style={{ borderColor: COLORS.green, color: COLORS.green }}>Retry local analysis</button></>
          : snapshotStale ? <><p className="text-sm mt-2">Saved findings need a fresh local check before you rely on them.</p><button type="button" disabled={!token || analysisBusy} onClick={runAnalysis} className="border px-3 py-2 text-sm mt-2" style={{ borderColor: COLORS.green, color: COLORS.green }}>Refresh local analysis</button></>
            : analysisFailed ? <p className="text-sm mt-2">The latest local check failed; saved findings were not updated.</p>
              : snapshot?.analysisState?.status !== "completed" ? <><p className="text-sm mt-2">No completed saved analysis is available.</p><button type="button" disabled={!token || analysisBusy} onClick={runAnalysis} className="border px-3 py-2 text-sm mt-2" style={{ borderColor: COLORS.green, color: COLORS.green }}>Run local analysis</button></>
              : <><p className="text-sm mt-2">Saved analysis found {(snapshot.findings || []).filter((finding) => ["error", "warning"].includes(finding.severity)).length} execution issue types across {Number(snapshot.scope?.analyzedRuns || 0).toLocaleString()} of {Number(snapshot.scope?.availableRuns || 0).toLocaleString()} runs. These findings are separate from recurring issue notices.</p>
                {snapshot.analysisState?.completedAt && <p className="text-xs mt-2" style={{ color: COLORS.sub }}>Analysis completed: {displayTime(snapshot.analysisState.completedAt)}{snapshot.analysisState.cutoff ? ` · started: ${displayTime(snapshot.analysisState.cutoff)}` : ""}</p>}
                {snapshot.scope?.complete === false && <p className="text-sm mt-2" style={{ color: COLORS.amber }}>This analysis covered only part of the captured activity.</p>}
                {actionable && <div className="mt-3"><strong>{ANALYSIS_LABELS[actionable.code] || actionable.code.replaceAll("_", " ")}</strong><p className="text-sm mt-1" style={{ color: COLORS.sub }}>{affectedRunLabel} in the saved analysis.</p><button type="button" onClick={() => onOpenFinding?.(actionable)} className="border px-3 py-2 text-sm mt-2" style={{ borderColor: COLORS.green, color: COLORS.green }}>Inspect affected runs →</button></div>}</>}
    </section>}
    {alertCount > 0 && <section className="border p-4 flex flex-wrap items-center justify-between gap-3" style={{ background: COLORS.panel, borderColor: COLORS.amber }}><div><strong>{modelWatch}</strong><p className="text-sm mt-1" style={{ color: COLORS.sub }}>Compare the affected metric and supporting calls before changing the policy.</p></div><button type="button" onClick={onOpenHistory} className="border px-3 py-2 text-sm" style={{ color: COLORS.green, borderColor: COLORS.border }}>Inspect model-call comparison</button></section>}
    {(!leadNotice || visible.length > 1 || page.nextOffset != null) && <section className="border p-5" style={{ background: COLORS.panel, borderColor: COLORS.border }}>
    <div className="flex flex-wrap justify-between gap-3 items-start">
      <div><div className="text-xs font-mono" style={{ color: COLORS.green }}>RECORDED FAILURES</div>
        <h2 className="text-lg font-semibold mt-1">{showReviewed ? "Reviewed issue history" : leadNotice ? "More recorded issues" : totalOpen ? `${totalOpen} recurring issue${totalOpen === 1 ? "" : "s"} to review` : "Recurring issue notices: none"}</h2></div>
      <button type="button" onClick={() => { void load(); void loadAnalysis(); void onRefreshStore?.(); }} className="border px-3 py-1 text-sm" style={{ borderColor: COLORS.border }}>Refresh</button>
    </div>
    <p className="text-sm mt-2" style={{ color: COLORS.sub }}>{scheduleActive || schedulePaused
      ? "Execution failures recorded since this plan was first activated. Reviewing a notice does not prove that a later run recovered."
      : "Recurring notices begin after you activate a plan. Earlier failures remain available in saved analysis above."}</p>
    {scheduleActive && !workerRecent && <p role="status" className="text-sm mt-3" style={{ color: COLORS.amber }}>Start <code>verdict-service</code> for this store to run future cycles. Saving a plan alone does not start it.</p>}
    {error && <p role="alert" className="text-sm mt-3" style={{ color: COLORS.amber }}>{error}</p>}
    <label className="flex gap-2 text-sm mt-4"><input type="checkbox" checked={showReviewed} onChange={(event) => selectReviewed(event.target.checked)} />Show acknowledged and dismissed history</label>
    <div className="space-y-3 mt-4">{visible.slice(leadNotice ? 1 : 0).map((issue) => <article key={issue.noticeId} className="border p-4" style={{ borderColor: issue.state === "open" ? "#805f2d" : COLORS.border }}>
      <div className="font-semibold">{LABELS[issue.checkCode] || issue.checkCode}</div>
      <div className="text-xs mt-1" style={{ color: COLORS.sub }}>{displayTime(issue.eventAt)} · {issue.state}</div>
      <p className="text-sm mt-2" style={{ color: COLORS.sub }}>Next: {issueNextCheck(issue)}</p>
      <div className="flex flex-wrap gap-3 mt-3 text-sm">
        <button type="button" onClick={() => onOpenEvidence(issue)} className="border px-3 py-1" style={{ color: COLORS.green, borderColor: COLORS.green }}>Open run evidence →</button>
        {issue.state === "open" && <><button type="button" disabled={!token || busy === issue.noticeId} onClick={() => review(issue, "acknowledge")} className="underline">Acknowledge</button>
          <button type="button" disabled={!token || busy === issue.noticeId} onClick={() => review(issue, "dismiss")} className="underline">Dismiss</button></>}
      </div>
      <details className="mt-3 text-sm"><summary className="cursor-pointer" style={{ color: COLORS.green }}>Prepare a local repair brief</summary>
        <div className="border-l pl-3 mt-3 space-y-2" style={{ borderColor: COLORS.border }}>
          <p><strong>Observed:</strong> {LABELS[issue.checkCode] || issue.checkCode} at {issue.eventAt}.</p>
          <p><strong>Evidence:</strong> run {issue.runId}{issue.evidenceKind === "run" ? ". Inspect the recorded run status before proposing a fix." : `, event ${issue.eventId}. Inspect the event before proposing a fix.`}</p>
          <p><strong>Cause:</strong> {issue.evidenceKind === "run" ? "not established by the recorded run status alone." : "not established by this event alone."}</p>
          <p><strong>Next check:</strong> {issueNextCheck(issue)}</p>
          <p><strong>Recovery:</strong> unverified. Acknowledge and dismiss only change review state.</p>
        </div>
      </details>
      {issue.evidenceKind !== "run" && <RecheckEvidence issue={issue} token={token} root={root} onOpenEvidence={onOpenEvidence}
        onRefresh={() => load()} />}
    </article>)}</div>
    {page.nextOffset != null && <button type="button" onClick={() => load(page.nextOffset)} className="border px-3 py-2 text-sm mt-4" style={{ borderColor: COLORS.border }}>Load more issues</button>}
    </section>}
    {leadNotice && visible.length === 1 && page.nextOffset == null && <button type="button" onClick={() => selectReviewed(true)} className="text-sm underline" style={{ color: COLORS.sub }}>View reviewed issue history</button>}
    <section><h3 className="text-lg font-semibold">Other watch areas</h3><p className="text-sm mt-1" style={{ color: COLORS.sub }}>Open the area that matches your question.</p>
      <div className="space-y-2 mt-3 text-sm">
        <div className="border p-4 flex flex-wrap items-center justify-between gap-3" style={{ background: COLORS.panel, borderColor: COLORS.border }}><div><strong>Token coverage</strong><p className="mt-1" style={{ color: COLORS.sub }}>Inspect reported token totals and coverage for captured sources.</p></div><button type="button" onClick={onOpenUsage} className="border px-3 py-2" style={{ borderColor: COLORS.border, color: COLORS.green }}>Inspect token coverage →</button></div>
        <div className="border p-4 flex flex-wrap items-center justify-between gap-3" style={{ background: COLORS.panel, borderColor: COLORS.border }}><div><strong>Model-call drift</strong><p className="mt-1" style={{ color: COLORS.sub }}>{modelWatch}. {Number(meta.monitorEligibleCalls || 0).toLocaleString()} completed eligible calls captured.</p></div><button type="button" onClick={onOpenHistory} className="border px-3 py-2" style={{ borderColor: COLORS.border, color: COLORS.green }}>Inspect model-call comparison →</button></div>
        <div className="border p-4 flex flex-wrap items-center justify-between gap-3" style={{ background: COLORS.panel, borderColor: COLORS.border }}><div><strong>Groups and clusters</strong><p className="mt-1" style={{ color: COLORS.sub }}>Narrow captured activity by its recorded source, provider, or model.</p></div><button type="button" onClick={onOpenGroups} className="border px-3 py-2" style={{ borderColor: COLORS.border, color: COLORS.green }}>Review groups →</button></div>
      </div>
    </section>
  </div>;
}
