import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, RefreshCw } from "lucide-react";
import { ScopedEvaluate } from "./ScopedEvaluate.jsx";
import { EvidenceExplain } from "./EvidenceExplain.jsx";

const color = {
  panel: "#111715", border: "#26332e", text: "#e7f0ec", sub: "#94a39d",
  faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b",
};

const RUN_PAGE_SIZE = 30;
const runTime = (value) => {
  if (typeof value !== "string" || !value) return "time unavailable";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric",
      year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(date)
    : "time unavailable";
};
const findingLabel = (code) => {
  const words = code.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};
const findingMessage = (message) => String(message || "").replace(
  /\b(\d+) (tool result|command|test execution)\(s\)/g,
  (_match, count, noun) => `${count} ${noun}${count === "1" ? "" : "s"}`,
);
const failureRechecks = {
  tool_error: ["Inspect the tool error or timeout and the surrounding run events.",
    "Repeat the operation only after reviewing its inputs and possible side effects. Record the later result to verify recovery."],
  command_failed: ["Inspect the command's recorded status, exit code, and surrounding evidence.",
    "Review its arguments and side effects before rerunning. Compare the later exit status and output."],
  test_failed: ["Inspect the test failure or timeout and any recorded assertion or output.",
    "Rerun the relevant test after a change and compare its later result."],
  business_outcome_failed: ["Inspect the reported failed outcome and its source evidence.",
    "Repeat or independently verify the outcome and record the later result before calling it recovered."],
};

export function Runs({
  url, focusRunIds = [], selectedRunId: routedRunId = null,
  findingCode = null, runIdsTruncated = false, onSelectRun = null, onShowAll = null,
  sourceKind = null, agentName = null, onClearGroup = null,
  evaluatorFingerprint = null, routedEventId = null, onOpenTrace = null,
  recentRevision = null, recentTurnId = null, recentEventId = null,
  onOpenRecentEvent = null,
  onEvaluateTurn = null, onDraftCase = null, onOpenRelatedRun = null,
  onBrowseRecordedGroup = null, noticeId = null, onOpenNoticeEvidence = null,
}) {
  const [state, setState] = useState({ scope: null, loading: true, error: null, data: null });
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState({ runId: null, loading: false, error: null, data: null });
  const [eventOffset, setEventOffset] = useState(0);
  const [turnOffset, setTurnOffset] = useState(0);
  const [turnEvaluatorInput, setTurnEvaluatorInput] = useState("");
  const [turnEvaluatorFilter, setTurnEvaluatorFilter] = useState(null);
  const [focusEventId, setFocusEventId] = useState(null);
  const [runOffset, setRunOffset] = useState(0);
  const [notice, setNotice] = useState({ id: null, loading: false, data: null, error: null });
  const listRequest = React.useRef(0);
  const detailRequest = React.useRef(0);
  const listScope = JSON.stringify([
    url, focusRunIds, evaluatorFingerprint, sourceKind, agentName, runOffset,
  ]);
  const selectedForScope = selected?.scope === listScope ? selected.runId : null;
  const load = React.useCallback(() => {
    const requestId = ++listRequest.current;
    setState((current) => ({
      scope: listScope, loading: true, error: null,
      data: current.scope === listScope ? current.data : null,
    }));
    const query = new URLSearchParams();
    query.set("view", "list");
    if (routedRunId || selectedForScope) query.set("selected_run_id", routedRunId || selectedForScope);
    focusRunIds.forEach((runId) => query.append("run_ids", runId));
    if (evaluatorFingerprint) query.set("evaluator_fingerprint", evaluatorFingerprint);
    if (!focusRunIds.length && sourceKind) query.set("source_kind", sourceKind);
    if (!focusRunIds.length && agentName) query.set("agent_name", agentName);
    if (!focusRunIds.length) {
      query.set("limit", String(RUN_PAGE_SIZE));
      query.set("offset", String(runOffset));
    }
    const listUrl = `${url}${url.includes("?") ? "&" : "?"}${query.toString()}`;
    fetch(listUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data) => {
        if (requestId === listRequest.current) {
          setState({ scope: listScope, loading: false, error: null, data });
        }
      })
      .catch((error) => {
        if (requestId === listRequest.current) {
          setState({ scope: listScope, loading: false, error: String(error), data: null });
        }
      });
  }, [listScope, routedRunId, selectedForScope]);
  useEffect(load, [load]);

  useEffect(() => {
    if (!noticeId) {
      setNotice({ id: null, loading: false, data: null, error: null });
      return;
    }
    const controller = new AbortController();
    setNotice({ id: noticeId, loading: true, data: null, error: null });
    const root = url.replace(/\/api\/runs$/, "");
    fetch(`${root}/api/control/issues/${encodeURIComponent(noticeId)}`, {
      credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
    }).then((response) => response.ok ? response.json() : Promise.reject(new Error("Linked issue unavailable")))
      .then((data) => {
        if (!controller.signal.aborted) setNotice({ id: noticeId, loading: false,
          data: data.noticeId === noticeId ? data : null,
          error: data.noticeId === noticeId ? null : "Linked issue unavailable" });
      }).catch((error) => {
        if (!controller.signal.aborted && error?.name !== "AbortError") {
          setNotice({ id: noticeId, loading: false, data: null, error: "Linked issue unavailable" });
        }
      });
    return () => controller.abort();
  }, [noticeId, url]);

  useEffect(() => {
    setSelected(null);
    setFocusEventId(routedEventId);
    setEventOffset(0);
    setTurnOffset(0);
  }, [routedEventId, routedRunId, listScope]);

  const visibleState = state.scope === listScope
    ? state : { loading: true, error: null, data: null };
  const runs = visibleState.data?.runs || [];
  const page = visibleState.data?.page || {
    available: visibleState.data?.summary?.available || 0,
    shown: runs.length,
    offset: focusRunIds.length ? 0 : runOffset,
    limit: RUN_PAGE_SIZE,
    truncated: !focusRunIds.length
      && runOffset + runs.length < (visibleState.data?.summary?.available || 0),
  };
  const selectedRunId = routedRunId || selectedForScope || runs[0]?.runId || null;
  const selectedRun = visibleState.data?.selectedRun?.runId === selectedRunId
    ? visibleState.data.selectedRun
    : visibleState.data?.selectedRun === undefined
      ? runs.find((run) => run.runId === selectedRunId)
      : null;
  const linkedEventId = findingCode && selectedRun?.findings
    .find((finding) => finding.code === findingCode)?.evidenceEventIds?.[0];
  const effectiveFocusEventId = focusEventId === null ? linkedEventId || null : focusEventId;
  const changeRunPage = (nextOffset) => {
    setSelected(null);
    onSelectRun?.(null);
    setFocusEventId(null);
    setEventOffset(0);
    setTurnOffset(0);
    setRunOffset(nextOffset);
  };
  useEffect(() => {
    if (!selectedRun) return;
    const controller = new AbortController();
    const requestId = ++detailRequest.current;
    setDetail((current) => ({
      runId: selectedRunId, loading: true, error: null,
      data: current.data?.runId === selectedRunId ? current.data : null,
    }));
    const eventFocus = effectiveFocusEventId ? `&event_id=${encodeURIComponent(effectiveFocusEventId)}` : "";
    const turnEvaluator = turnEvaluatorFilter ? `&turn_evaluator_fingerprint=${turnEvaluatorFilter}` : "";
    fetch(`${url}/${encodeURIComponent(selectedRunId)}?event_limit=100&event_offset=${eventOffset}&turn_limit=20&turn_offset=${turnOffset}${eventFocus}${turnEvaluator}`, {
      credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((data) => {
        if (data.runId !== selectedRunId) throw new Error("Run detail did not match selection");
        if (requestId === detailRequest.current && !controller.signal.aborted) {
          setDetail({ runId: selectedRunId, loading: false, error: null, data });
        }
      })
      .catch((error) => {
        if (requestId === detailRequest.current && !controller.signal.aborted
            && error?.name !== "AbortError") {
          setDetail({ runId: selectedRunId, loading: false, error: String(error), data: null });
        }
      });
    return () => controller.abort();
  }, [eventOffset, effectiveFocusEventId, selectedRun?.runId, turnOffset, turnEvaluatorFilter, url]);

  if (visibleState.error) return <Notice icon={AlertTriangle} text={`Runs unavailable: ${visibleState.error}`} />;
  if (!visibleState.data) return <Notice icon={RefreshCw} text="Loading agent runs…" />;
  if (!runs.length && page.available === 0) {
    if (sourceKind || agentName) return <section className="border p-5 text-sm" style={{ borderColor: color.border, background: color.panel }}><p>No runs are recorded in this group for the current tenant.</p><button type="button" onClick={onClearGroup} className="underline mt-3" style={{ color: color.green }}>Browse all runs →</button></section>;
    return <Notice icon={focusRunIds.length ? AlertTriangle : CheckCircle2}
      text={focusRunIds.length
        ? "The requested agent run is unavailable in this tenant."
        : "No agent runs captured yet. Capture local history or an SDK Agent Run, then refresh."} />;
  }
  const focusedFinding = findingCode && selectedRun?.findings?.find((finding) => finding.code === findingCode);
  return (
    <div>
      {noticeId && <NoticeContext notice={notice.id === noticeId ? notice : { loading: true }}
        selectedRunId={selectedRunId} onOpenEvidence={onOpenNoticeEvidence} />}
      {focusedFinding && <FocusedIssueStory run={selectedRun} finding={focusedFinding}
        detail={detail.runId === selectedRunId ? detail : { loading: true, data: null }}
        focusEventId={effectiveFocusEventId} affectedRuns={focusRunIds.length} url={url}
        onOpenTrace={onOpenTrace}
        onBrowseRecordedGroup={onBrowseRecordedGroup} />}
      <details key={focusedFinding ? `finding:${findingCode}` : "run-browser"}
        open={focusedFinding ? undefined : true} className={focusedFinding ? "verdict-investigation-details" : "verdict-investigation-browser"}>
        <summary>{focusedFinding ? "Browse affected runs and full event details" : "Run browser"}</summary>
    <div className="grid lg:grid-cols-[360px_minmax(0,1fr)] gap-4">
      <section className={`border ${focusRunIds.length ? "order-2 lg:order-1" : "order-1"}`} style={{ borderColor: color.border, background: color.panel }}>
        {(sourceKind || agentName) && <div className="p-3 border-b text-sm flex flex-wrap justify-between gap-2" style={{ borderColor: color.border }}><span>Recorded {[sourceKind && `source: ${sourceKind}`, agentName && `agent: ${agentName}`].filter(Boolean).join(" · ")} · {page.available.toLocaleString()} runs</span><button type="button" onClick={onClearGroup} className="underline" style={{ color: color.green }}>Clear group</button></div>}
        {focusRunIds.length > 0 && <div className="p-3 border-b text-xs flex items-center justify-between gap-3" style={{ borderColor: color.amber, color: color.amber }}>
        <span>{findingCode ? `${findingLabel(findingCode)}: showing` : "Selected run: showing"} {runs.length} of {focusRunIds.length}{runIdsTruncated ? "+" : ""} {findingCode ? "affected runs" : "runs"}{visibleState.data?.filter?.complete === false ? " (some are unavailable in this tenant)" : ""}.</span>
          <button className="underline shrink-0" onClick={() => { setRunOffset(0); onShowAll?.(); }}>Show all runs</button>
        </div>}
        <div className="p-4 border-b flex items-center justify-between" style={{ borderColor: color.border }}>
          <div>
            <div className="font-semibold">Agent runs</div>
            <div className="text-xs mt-1" style={{ color: color.sub }}>
              {focusRunIds.length
                ? `${visibleState.data.summary.shown} of ${visibleState.data.summary.available} runs`
                : `Showing ${page.shown ? page.offset + 1 : 0}–${page.offset + page.shown} of ${page.available} runs`}
            </div>
          </div>
          <button onClick={load} title="Refresh runs"><RefreshCw size={15} /></button>
        </div>
        {runs.map((run) => {
          const active = selectedRunId === run.runId;
          return (
            <button key={run.runId} onClick={() => { setSelected({ scope: listScope, runId: run.runId }); onSelectRun?.(run.runId); setFocusEventId(null); setEventOffset(0); setTurnOffset(0); }}
              className="w-full text-left p-4 border-b flex items-start gap-3"
              style={{ borderColor: color.border, background: active ? "#18221e" : "transparent" }}>
              <ChevronRight size={15} style={{ marginTop: 2, color: active ? color.green : color.faint }} />
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold">{run.agentName || run.sourceKind}</span>
                <span className="block text-xs mt-1" style={{ color: color.sub }}>{run.sourceKind}</span>
                <span className="block text-xs mt-1 truncate" style={{ color: color.faint }}>{runTime(run.startedAt)}</span>
              </span>
            </button>
          );
        })}
        {!runs.length && <div className="p-4 text-sm" style={{ color: color.sub }}>No runs on this page.</div>}
        {!focusRunIds.length && (page.available > page.limit || page.offset > 0) && <div className="p-3 flex items-center justify-between gap-2 text-xs" style={{ color: color.sub }}>
          <button disabled={visibleState.loading || page.offset === 0} onClick={() => changeRunPage(Math.max(0, page.offset - page.limit))} className="border px-3 py-1">Previous</button>
          <button disabled={visibleState.loading || !page.truncated} onClick={() => changeRunPage(page.offset + page.limit)} className="border px-3 py-1">Next</button>
        </div>}
      </section>
      <RunDetail url={url} run={selectedRun} detail={detail.runId === selectedRunId ? detail : { loading: true, error: null, data: null }} findingCode={findingCode} prioritizeDetail={focusRunIds.length > 0} otherAgentRunsVisible={runs.some((item) => item.runId !== selectedRunId && item.agentName && item.agentName !== selectedRun?.agentName)} onEventPage={(offset) => { setFocusEventId(""); setEventOffset(offset); }} onTurnPage={setTurnOffset} onFocusEvent={(eventId) => { setFocusEventId(eventId); setEventOffset(0); }} focusEventId={effectiveFocusEventId} onOpenTrace={onOpenTrace} onEvaluateTurn={onEvaluateTurn} onDraftCase={onDraftCase} onOpenRelatedRun={onOpenRelatedRun} onBrowseRecordedGroup={onBrowseRecordedGroup} turnEvaluatorInput={turnEvaluatorInput} onTurnEvaluatorInput={setTurnEvaluatorInput} turnEvaluatorFilter={turnEvaluatorFilter} onTurnEvaluatorFilter={setTurnEvaluatorFilter} recentReference={recentRevision && recentTurnId && recentEventId ? { revision: recentRevision, turnId: recentTurnId, eventId: recentEventId } : null} onOpenRecentEvent={onOpenRecentEvent} />
    </div>
      </details>
    </div>
  );
}

function NoticeContext({ notice, selectedRunId, onOpenEvidence }) {
  if (notice.loading) return <Notice icon={RefreshCw} text="Loading linked issue and recorded rechecks…" />;
  if (notice.error || !notice.data) return <Notice icon={AlertTriangle} text="Linked issue unavailable. The run evidence below remains available." />;
  const issue = notice.data;
  const rechecks = Array.isArray(issue.rechecks) ? issue.rechecks : [];
  if (selectedRunId !== issue.runId && !rechecks.some((item) => item.runId === selectedRunId)) return null;
  return <section className="border p-4 mb-4 text-sm space-y-2" style={{ borderColor: color.amber, background: color.panel }}>
    <h2 className="font-semibold">Recorded issue and later checks</h2>
    <p>{findingLabel(issue.checkCode)} at {runTime(issue.eventAt)}. Review the failed evidence and any attached later pass together. A later pass does not establish overall recovery.</p>
    <div className="flex flex-wrap gap-3">
      <button type="button" className="underline" style={{ color: color.green }}
        onClick={() => onOpenEvidence?.(issue.runId, issue.eventId, issue.recentRevision, issue.recentTurnId)}>Open failed evidence →</button>
      {rechecks.map((item) => <button type="button" key={`${item.runId}:${item.eventId}`}
        className="underline" style={{ color: color.green }}
        onClick={() => onOpenEvidence?.(item.runId, item.eventId, item.recentRevision, item.recentTurnId)}>
        Open later recorded pass · {runTime(item.eventAt)} →
      </button>)}
    </div>
    {!rechecks.length && <p style={{ color: color.sub }}>No later passing check is attached to this issue.</p>}
  </section>;
}

function FocusedIssueStory({ run, finding, detail, focusEventId, affectedRuns, url, onOpenTrace, onBrowseRecordedGroup }) {
  const event = detail.data?.events?.find((item) => item.eventId === focusEventId);
  const linkedEvents = finding.evidenceEventIds || [];
  const recommendation = (run.recommendations || []).find((item) =>
    item.evidenceEventIds?.some((eventId) => linkedEvents.includes(eventId)));
  const recheck = failureRechecks[finding.code];
  return <div className="verdict-issue-story">
    <div className="verdict-issue-story-main">
      <section className="verdict-issue-story-card">
        <span className="verdict-issue-story-label">{finding.severity === "error" ? "RECORDED FAILURE" : "RECORDED FINDING"}</span>
        <div className="verdict-issue-story-head"><h2>What happened</h2><span>{run.sourceKind} · {runTime(run.startedAt)}</span></div>
        <p>{findingMessage(finding.message)}</p>
        <div className="verdict-issue-story-facts">
          <div><b>RUN</b><span>{run.agentName || run.sourceKind} · {run.turnCount} {run.sourcePartial ? "recorded prefix turns" : run.turnCount === 1 ? "turn" : "turns"} · {run.eventCount} {run.sourcePartial ? "recorded prefix events" : run.eventCount === 1 ? "event" : "events"}</span></div>
          <div><b>EVENT</b><span>{event ? `#${event.timelineIndex + 1} · ${event.type.replaceAll("_", " ")} · ${event.status}` : detail.loading ? "Loading linked evidence…" : linkedEvents.length ? "Linked event unavailable in this run" : "No event-level evidence recorded"}</span></div>
          {affectedRuns > 1 && <div><b>SCOPE</b><span>{affectedRuns} affected runs · this run is shown first</span></div>}
        </div>
      </section>
      <section className="verdict-issue-story-card">
        <h2>Recorded evidence</h2>
        {event ? <EventRow event={event} root={url.replace(/\/api\/runs$/, "")}
          runId={run.runId} focused idPrefix="lead-" onOpenTrace={onOpenTrace} />
          : <p>{detail.loading ? "Loading the linked event…" : "The linked event is unavailable here. Open the full run details to review what was captured."}</p>}
        <small>Verdict has not inferred a cause or a later recovery from this event.</small>
      </section>
    </div>
    <aside className="verdict-issue-story-aside">
      <section className="verdict-issue-story-card">
        <h2>Suggested next action</h2>
        <p>{recommendation?.action || recheck?.[0] || "Inspect the recorded evidence before deciding what to change."}</p>
        {recommendation?.tradeoff && <small>{recommendation.tradeoff}</small>}
        {recheck && <><h3>After the fix</h3><p>{recheck[1]}</p></>}
        {onBrowseRecordedGroup && run.sourceKind && <><button type="button" className="underline text-sm mt-3 text-left" style={{ color: color.green }} onClick={() => onBrowseRecordedGroup({ sourceKind: run.sourceKind, agentName: run.agentName || null })}>{`Compare runs with the same recorded ${run.agentName ? "source and agent" : "source"} →`}</button><small>Matching labels help find other runs; they do not establish a shared cause or task.</small></>}
      </section>
      <section className="verdict-issue-story-card">
        <h2>Need deeper analysis?</h2>
        <p>Review the captured event or ask for an optional AI explanation after checking its evidence and cost.</p>
        <EvidenceExplain root={url.replace(/\/api\/runs$/, "")} scope="run" runId={run.runId} />
      </section>
    </aside>
  </div>;
}

const TOKEN_COMPONENTS = [
  ["inputTokens", "input tokens"],
  ["cachedInputTokens", "cached input tokens"],
  ["cacheWriteInputTokens", "cache-write input tokens"],
  ["outputTokens", "output tokens"],
  ["reasoningOutputTokens", "reasoning output tokens"],
];

function availableTokenComponents(usage) {
  return TOKEN_COMPONENTS.flatMap(([name, label]) => (
    usage?.[name] == null ? [] : [`${Number(usage[name]).toLocaleString()} ${label}`]
  ));
}

function sourceTokenSummary(usage) {
  if (usage.totalTokens != null) {
    return `${Number(usage.totalTokens).toLocaleString()} source-reported tokens${usage.state === "partial" ? " (partial)" : ""}`;
  }
  const components = availableTokenComponents(usage);
  if (usage.state === "partial") {
    return components.length
      ? `${components.join(" · ")} (partial; total unavailable)`
      : "source token usage partially captured; total unavailable";
  }
  return "source tokens not captured";
}

function turnTokenSummary(usage) {
  if (usage?.totalTokens != null) {
    const basis = usage.basis ? ` · ${usage.basis.replaceAll("_", " ")}` : "";
    return `${Number(usage.totalTokens).toLocaleString()} total${basis}`;
  }
  const components = availableTokenComponents(usage);
  if (components.length) {
    const basis = usage?.basis ? ` · ${usage.basis.replaceAll("_", " ")}` : "";
    return `${components.join(" · ")} · partial; total unavailable${basis}`;
  }
  return "not captured";
}

function RecentLocalEvents({ url, runId, recentReference = null, onOpenRecentEvent = null, autoAnalyze = false }) {
  const [state, setState] = useState({ status: "idle", data: null });
  const [fullPage, setFullPage] = useState({ status: "idle", data: null });
  const [fullAnalysis, setFullAnalysis] = useState({ status: "idle", data: null });
  const [failuresOnly, setFailuresOnly] = useState(false);
  const requestId = useRef(0);
  const analysisRequestId = useRef(0);
  const fullPageRef = useRef(null);
  useEffect(() => {
    requestId.current += 1;
    analysisRequestId.current += 1;
    setState({ status: "idle", data: null });
    setFullPage({ status: "idle", data: null });
    setFullAnalysis({ status: "idle", data: null });
    setFailuresOnly(false);
    if (recentReference || autoAnalyze) void load(recentReference);
  }, [runId, url, autoAnalyze, recentReference?.revision, recentReference?.turnId, recentReference?.eventId]);
  async function load(reference = null) {
    const current = ++requestId.current;
    setFailuresOnly(false);
    setFullPage({ status: "idle", data: null });
    setFullAnalysis({ status: "idle", data: null });
    analysisRequestId.current += 1;
    setState({ status: "loading", data: null });
    try {
      const query = reference ? `?${new URLSearchParams({ revision: reference.revision,
        turn_id: reference.turnId, event_id: reference.eventId })}` : "";
      const response = await fetch(`${url}/${encodeURIComponent(runId)}/recent-local-events${query}`, {
        credentials: "same-origin", headers: { Accept: "application/json" },
      });
      if (response.status === 409) throw new Error("stale_revision");
      if (!response.ok) throw new Error("recent sample unavailable");
      const data = await response.json();
      if (current === requestId.current && data.runId === runId) {
        setState({ status: "ready", data });
      }
    } catch (error) {
      if (current === requestId.current) setState({ status: error?.message === "stale_revision" ? "stale" : "error", data: null });
    }
  }
  const readySample = state.status === "ready" && state.data?.runId === runId
    ? state.data : null;
  useEffect(() => {
    if (autoAnalyze && readySample?.sourceRevision && fullAnalysis.status === "idle") {
      void loadFullAnalysis();
    }
  }, [autoAnalyze, readySample?.sourceRevision, fullAnalysis.status, runId]);
  useEffect(() => {
    if (fullPage.status === "ready") fullPageRef.current?.scrollIntoView?.({ block: "start" });
  }, [fullPage.status, fullPage.data]);
  const currentWindow = readySample?.schema === "local-recent-window-v2";
  const sampledEvents = Array.isArray(readySample?.events) ? readySample.events : [];
  const latestSource = readySample?.latestSample;
  const newestRecordsAvailable = latestSource?.status === "sampled";
  const sampledTurns = currentWindow && Array.isArray(readySample?.turns)
    ? readySample.turns : [];
  const windowFindings = currentWindow && Array.isArray(readySample?.findings)
    ? readySample.findings : [];
  const reportedFailures = sampledEvents.filter((event) =>
    event.status === "failed" || event.status === "timed_out");
  const visibleEvents = failuresOnly ? reportedFailures : sampledEvents;
  const historicalEvent = readySample?.historicalFocusedEvent === true
    ? readySample.focusedEvent : null;
  const findingRevision = (finding) => sampledEvents.find((event) =>
    event.eventId === finding.eventId)?.firstSeenRevision || readySample.sourceRevision;
  async function loadFullPage(after = null, requestedEventId = null) {
    if (!readySample?.sourceRevision) return;
    const current = ++requestId.current;
    setFullPage({ status: "loading", data: null });
    try {
      const params = new URLSearchParams({ revision: readySample.sourceRevision, limit: "100" });
      if (after) {
        params.set("after_ordinal", String(after.sourceOrdinal));
        params.set("after_event_id", after.eventId);
      }
      const response = await fetch(`${url}/${encodeURIComponent(runId)}/local-events?${params}`, {
        credentials: "same-origin", headers: { Accept: "application/json" },
      });
      if (response.status === 409) {
        const body = await response.json();
        throw new Error(body.error === "partial_projection" ? "partial" : "stale");
      }
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (current === requestId.current && data.sourceRevision === readySample.sourceRevision
          && data.coverage === "complete_recorded") {
        setFullPage({ status: "ready", data,
          focusedEventId: data.events?.some((event) => event.eventId === requestedEventId)
            ? requestedEventId : null });
      }
    } catch (error) {
      if (current === requestId.current) setFullPage({ status: error?.message || "unavailable", data: null });
    }
  }
  async function loadFullAnalysis() {
    if (!readySample?.sourceRevision) return;
    const current = ++analysisRequestId.current;
    setFullAnalysis({ status: "loading", data: null });
    try {
      const response = await fetch(`${url}/${encodeURIComponent(runId)}/recorded-analysis`, {
        credentials: "same-origin", headers: { Accept: "application/json" },
      });
      if (response.status === 409) throw new Error("partial");
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (current === analysisRequestId.current && data.sourceRevision === readySample.sourceRevision
          && data.coverage === "complete_recorded") setFullAnalysis({ status: "ready", data });
      else if (current === analysisRequestId.current) setFullAnalysis({ status: "stale", data: null });
    } catch (error) {
      if (current === analysisRequestId.current) setFullAnalysis({ status: error?.message || "unavailable", data: null });
    }
  }
  const fullReport = fullAnalysis.status === "ready" && <div className="border p-3 mb-4 space-y-2" style={{ borderColor: color.amber }}>
    <h3 className="font-semibold">Recorded-run analysis</h3>
    <p>Analysis of {Number(fullAnalysis.data.recordedTurns).toLocaleString()} recorded {Number(fullAnalysis.data.recordedTurns) === 1 ? "turn" : "turns"} and {Number(fullAnalysis.data.recordedEvents).toLocaleString()} recorded events in this source revision. This does not establish task success or private reasoning.</p>
    {fullAnalysis.data.recommendations?.filter((item) => item.status === "suggested").slice(0, 1).map((item) => <div key={item.code} className="border p-3" style={{ borderColor: color.amber }}>
      <strong>Suggested next step</strong>
      <p>{item.observation} {item.action}</p>
      {item.evidenceEventIds?.length > 0 && <button type="button" className="underline mt-1" onClick={() => loadFullPage(item.firstEvidenceCursor, item.evidenceEventIds[0])}>Inspect supporting event</button>}
    </div>)}
    <p>{Number(fullAnalysis.data.metrics?.tool_calls || 0).toLocaleString()} tool calls · {Number(fullAnalysis.data.metrics?.tool_errors || 0).toLocaleString()} reported tool errors · {Number(fullAnalysis.data.metrics?.command_failures || 0).toLocaleString()} reported command failures</p>
    {fullAnalysis.data.findings?.length ? fullAnalysis.data.findings.map((finding, index) => <div key={`${finding.code}:${index}`} className="border p-2" style={{ borderColor: color.border }}>
      <strong>{String(finding.code).replaceAll("_", " ")}</strong> · {finding.message}
      {finding.evidenceEventIds?.length > 0 && <button type="button" className="underline ml-2" onClick={() => loadFullPage(finding.firstEvidenceCursor, finding.evidenceEventIds[0])}>Inspect recorded evidence</button>}
    </div>) : <p>No deterministic finding in the fully recorded events. This does not establish task success.</p>}
    {fullAnalysis.data.findingsTruncated && <p>Additional finding types were omitted from this view.</p>}
  </div>;
  return <section className="border p-3 mt-4 text-sm" style={{ borderColor: color.border }}>
    {fullReport}
    <h3 className="font-semibold">Recent local-history sample</h3>
    <p className="text-xs mt-1" style={{ color: color.sub }}>
      {!readySample
        ? "This source history is partial. Inspect the recent sample to determine what was retained when it is available; run-wide totals and no-issue claims remain unavailable."
        : newestRecordsAvailable
        ? "The canonical run contains a bounded historical prefix. Open the newest source-record sample below to inspect the latest available records. It may overlap the prefix and cannot establish full-session totals, alerts, or task outcome."
        : latestSource
        ? "The canonical run contains a bounded historical prefix. The newest source records are unavailable; full-session totals, alerts, and task outcome cannot be established."
        : !currentWindow
        ? "This legacy recent sample is for inspection only; it is not counted in analysis, monitoring, or evaluation."
        : readySample?.durableFacts === true
          ? "This recent view selects bounded turns and events. Explicit recorded failures can enter recurring issue monitoring. Browse recorded events for a verified complete captured page when available; this recent selection alone cannot establish run-wide issue totals or a no-issue claim."
          : "Recent source evidence is bounded to selected turns and events. Run-wide totals and alerts are unavailable for this partial history. Complete selected turns can have recorded-issue checks; the recent window does not enter recurring monitoring or LLM evaluation."}
    </p>
    <button type="button" className="underline mt-2" style={{ color: color.green }}
      disabled={state.status === "loading"} onClick={() => load()}>View recent sample</button>
    {state.status === "loading" && <p role="status">Loading recent events…</p>}
    {state.status === "error" && <p role="alert" style={{ color: color.amber }}>No current recent sample is available for this run. The canonical events above remain available.</p>}
    {state.status === "stale" && <p role="alert" style={{ color: color.amber }}>This exact recent-event link refers to an older source revision. Load the current window to inspect the latest captured evidence.</p>}
    {readySample && <div className="mt-3 space-y-2" style={{ maxHeight: "28rem", overflowY: "auto" }}>
      <p>Captured {runTime(readySample.capturedAt)} · {newestRecordsAvailable ? "A bounded prefix and newest source-record sample are available; full-session counts are unknown" : latestSource ? "Bounded prefix captured; newest source records are unavailable and full-session counts are unknown" : `${sampledEvents.length} of ${Number(readySample.sourceEventCount).toLocaleString()} source events shown across ${Number(readySample.sourceRecordCount).toLocaleString()} source records`} · revision {String(readySample.sourceRevision).slice(0, 12)}. {readySample.omittedMiddleEvents > 0 && !latestSource ? `${Number(readySample.omittedMiddleEvents).toLocaleString()} source events outside the canonical prefix and this sample. Browse recorded events below when full capture is available.` : newestRecordsAvailable ? "The sample may overlap the canonical prefix." : ""}</p>
      {latestSource && <section aria-label="Newest sampled source records" className="border p-2" style={{ borderColor: color.amber }}>
        <h4 className="font-semibold">{newestRecordsAvailable ? "Newest sampled source records" : "Newest source records unavailable"}</h4>
        {newestRecordsAvailable && <p className="text-xs mt-1" style={{ color: color.sub }}>Inspection only. This newest source sample may overlap the canonical prefix. It adds nothing to token totals, issue counts, alerts, or evaluation.</p>}
        {latestSource.status === "sampled"
          ? latestSource.records.map((record, index) => <div key={index} className="border-t py-2 text-xs" style={{ borderColor: color.border }}>
              <strong>{record.kind}</strong> · {record.at || "time unavailable"}
              {record.preview && <p className="whitespace-pre-wrap break-words mt-1">{record.preview}</p>}
            </div>)
          : <p className="text-xs mt-2" role="status">{({
              unavailable_decoded_limit: "The archive expands beyond the 512 MiB newest-sample limit.",
              unavailable_malformed_record: "The newest complete source records could not be read.",
              unavailable_no_records: "No complete source records were found in the newest window.",
            })[latestSource.status] || "The newest source records could not be sampled."}</p>}
      </section>}
      {currentWindow && !latestSource && <div className="border p-2" style={{ borderColor: color.border }}>
        <button type="button" className="underline" style={{ color: color.green }}
          disabled={fullPage.status === "loading"} onClick={() => loadFullPage()}>
          Browse recorded events
        </button>
        <button type="button" className="underline ml-4" style={{ color: color.green }}
          disabled={fullAnalysis.status === "loading"} onClick={loadFullAnalysis}>
          Analyze recorded run
        </button>
        {fullAnalysis.status === "loading" && <p role="status">Analyzing recorded run…</p>}
        {fullAnalysis.status === "partial" && <p role="status">A complete recorded-run analysis is unavailable for this partial source. Inspect the bounded sample.</p>}
        {fullAnalysis.status === "stale" && <p role="alert">The source changed. Reload the recent sample before analyzing.</p>}
        {fullAnalysis.status === "unavailable" && <p role="alert">Recorded-run analysis could not be loaded. Retry after checking the source.</p>}
        {fullPage.status === "loading" && <p role="status">Loading recorded events…</p>}
        {fullPage.status === "partial" && <p role="status">This source has a bounded recent sample; a complete recorded-event page is unavailable.</p>}
        {fullPage.status === "stale" && <p role="alert">The source changed. Reload the recent sample before browsing.</p>}
        {fullPage.status === "unavailable" && <p role="alert">Recorded events could not be loaded. Retry from this sample.</p>}
        {fullPage.status === "ready" && <div ref={fullPageRef} aria-label="Recorded event page" className="mt-2 space-y-2">
          <p>Complete recorded-event coverage for this captured source revision · {Number(fullPage.data.sourceEventCount).toLocaleString()} events. This does not prove the agent completed its task successfully.</p>
          {fullPage.data.events.map((event) => <details key={`${event.sourceOrdinal}:${event.eventId}`} open={fullPage.focusedEventId === event.eventId} className="border p-2" style={{ borderColor: color.border }}>
            <summary>Source event #{event.sourceOrdinal + 1} · {String(event.type).replaceAll("_", " ")} · {event.status}</summary>
            {onOpenRecentEvent && <button type="button" className="underline text-xs mt-2" onClick={() => onOpenRecentEvent({ runId, revision: event.firstSeenRevision || readySample.sourceRevision, turnId: event.turnId, eventId: event.eventId })}>Open exact event link</button>}
            <pre className="text-xs mt-2 whitespace-pre-wrap break-all">{JSON.stringify(event.attributes, null, 2)}</pre>
          </details>)}
          {fullPage.data.nextCursor && <button type="button" className="underline" onClick={() => loadFullPage(fullPage.data.nextCursor)}>Next 100 recorded events</button>}
        </div>}
      </div>}
      {historicalEvent && <div className="border p-2" style={{ borderColor: color.amber }}>
        <p>Retained event from an earlier source revision. It is outside the current recent window.</p>
        <details open className="mt-2"><summary>Source event #{historicalEvent.sourceOrdinal + 1} · {historicalEvent.type.replaceAll("_", " ")} · {historicalEvent.status}</summary>
          <pre className="text-xs mt-2 whitespace-pre-wrap break-all">{JSON.stringify(historicalEvent.attributes, null, 2)}</pre>
        </details>
      </div>}
      {sampledEvents.length > 0 && <p>{reportedFailures.length
        ? `${reportedFailures.length} of the ${sampledEvents.length} sampled events report failed or timed-out status. This partial sample is not a complete issue count.`
        : "No sampled events report failed or timed-out status. Other source events may be omitted."}</p>}
      {currentWindow && !latestSource && <section aria-label="Recent turn coverage" className="border p-2" style={{ borderColor: color.border }}>
        <p>{windowFindings.length} recorded {windowFindings.length === 1 ? "issue" : "issues"} in complete selected turns. These checks cover this window only.</p>
        {windowFindings.map((finding, index) => <p key={`${finding.eventId}:${index}`} className="text-xs mt-1">
          {String(finding.code).replaceAll("_", " ")} · event {String(finding.eventId).slice(0, 12)}
          {onOpenRecentEvent && <button type="button" className="underline ml-2" onClick={() => onOpenRecentEvent({ runId, revision: findingRevision(finding), turnId: finding.turnId, eventId: finding.eventId })}>Open exact event link</button>}
        </p>)}
        {sampledTurns.map((turn) => <p key={turn.turnId} className="text-xs mt-1" style={{ color: color.sub }}>
          Turn #{Number(turn.sourceSequence) + 1} · {turn.eligibleForFindings ? "complete selection" : "partial selection"} · {Number(turn.selectedEventCount)} of {Number(turn.sourceEventCount)} source events shown
        </p>)}
      </section>}
      {reportedFailures.length > 0 && reportedFailures.length < sampledEvents.length &&
        <button type="button" className="underline" style={{ color: color.green }}
          aria-pressed={failuresOnly} onClick={() => setFailuresOnly((current) => !current)}>
          {failuresOnly ? "Show all sampled events" : "Show reported failures"}
        </button>}
      {visibleEvents.map((event) => <details key={`${event.sourceOrdinal}:${event.eventId}`} open={readySample.focusedEvent?.eventId === event.eventId} className="border p-2" style={{ borderColor: color.border }}>
        <summary className="cursor-pointer">Source event #{event.sourceOrdinal + 1} · {event.type.replaceAll("_", " ")} · {event.status} · {runTime(event.occurredAt)}</summary>
        <p className="text-xs mt-2" style={{ color: color.sub }}>{event.provenance} · {event.privacy}</p>
        {currentWindow && onOpenRecentEvent && <button type="button" className="underline text-xs mt-2" onClick={() => onOpenRecentEvent({ runId, revision: event.firstSeenRevision || readySample.sourceRevision, turnId: event.turnId, eventId: event.eventId })}>Open exact event link</button>}
        <pre className="text-xs mt-2 whitespace-pre-wrap break-all">{JSON.stringify(event.attributes, null, 2)}</pre>
      </details>)}
    </div>}
  </section>;
}

function RunDetail({ url, run, detail, findingCode, prioritizeDetail = false, otherAgentRunsVisible = false, onEventPage, onTurnPage, onFocusEvent, focusEventId, onOpenTrace, onEvaluateTurn, onDraftCase, onOpenRelatedRun, onBrowseRecordedGroup, turnEvaluatorInput, onTurnEvaluatorInput, turnEvaluatorFilter, onTurnEvaluatorFilter, recentReference = null, onOpenRecentEvent = null }) {
  if (!run) return null;
  const metrics = run.metrics || {};
  const sourceUsage = run.sourceTokenUsage || { totalTokens: null, state: "not_captured" };
  const sourceTokens = sourceTokenSummary(sourceUsage);
  const localPartial = run.sourcePartial && ["codex", "claude-code"].includes(run.sourceKind);
  const PrefixShell = localPartial ? "details" : React.Fragment;
  const terminalFailure = run.status === "failed" || run.status === "timed_out";
  const focusedFinding = findingCode ? run.findings.find((finding) => finding.code === findingCode) : null;
  const findings = focusedFinding ? [focusedFinding] : run.findings;
  const otherFindings = focusedFinding ? run.findings.filter((finding) => finding !== focusedFinding) : [];
  const focusedEvidence = new Set(focusedFinding?.evidenceEventIds || []);
  const recommendations = run.recommendations || [];
  const linkedRecommendations = focusedFinding
    ? recommendations.filter((item) => item.evidenceEventIds?.some((eventId) => focusedEvidence.has(eventId)))
    : recommendations;
  const otherRecommendations = focusedFinding
    ? recommendations.filter((item) => !linkedRecommendations.includes(item)) : [];
  const selectedEvent = focusEventId ? detail.data?.events?.find((event) => event.eventId === focusEventId) : null;
  const linkedEvidenceIds = (run.findings.find((finding) => finding.evidenceEventIds?.includes(focusEventId)) || focusedFinding)?.evidenceEventIds || [];
  const selectedEvidenceIndex = linkedEvidenceIds.indexOf(focusEventId);
  return (
    <section className={`border p-5 min-w-0 ${prioritizeDetail ? "order-1 lg:order-2" : "order-2"}`} style={{ borderColor: color.border, background: color.panel }}>
      <div className="flex flex-wrap justify-between gap-3">
        <div>
          <div className="text-xs font-mono" style={{ color: color.green }}>AGENT RUN</div>
          <div className="font-semibold mt-1">{run.agentName || run.sourceKind}</div>
          <div className="text-xs mt-1" style={{ color: color.sub }}>
            {run.sourcePartial ? "Recorded prefix outcome" : "Run outcome"}: {run.status === "unknown" ? "not recorded by source" : run.status} · {[run.serviceName, run.environment, run.agentVersion].filter(Boolean).join(" · ") || run.sourceKind}
          </div>
        </div>
        <div className="text-xs" style={{ color: color.sub }}>
          {sourceTokens} · {metrics.tool_calls || 0} {localPartial ? "tool calls in stored prefix" : "observed tool calls"}
        </div>
      </div>
      {onBrowseRecordedGroup && (run.agentName || run.sourceKind) && <button
        type="button" className="underline text-sm mt-3 text-left" style={{ color: color.green }}
        onClick={() => onBrowseRecordedGroup({ agentName: run.agentName || null, sourceKind: run.sourceKind })}>
        {run.agentName ? `Browse ${run.sourceKind} runs by ${run.agentName}` : `Browse ${run.sourceKind} runs`} →
      </button>}
      {detail.data && (detail.data.parentRunId || detail.data.children?.length > 0) && <section className="border p-3 mt-4 text-sm" style={{ borderColor: color.border }} aria-label="Related agent runs">
        <div className="font-semibold">Related agent runs</div>
        <p className="text-xs mt-1" style={{ color: color.sub }}>These links come from recorded parent run IDs. Other messages or handoffs are not inferred from timing.</p>
        {detail.data.parentRunId && detail.data.parentAvailable !== false && <button type="button" className="block underline mt-2" style={{ color: color.green }} onClick={() => onOpenRelatedRun?.(detail.data.parentRunId)}>Open recorded parent run</button>}
        {detail.data.parentRunId && detail.data.parentAvailable === false && <p className="text-xs mt-2" style={{ color: color.amber }}>A parent run ID was recorded, but that run is unavailable in this tenant.</p>}
        {detail.data.children?.map((child) => <button key={child.runId} type="button" className="block underline mt-2 text-left" style={{ color: color.green }} onClick={() => onOpenRelatedRun?.(child.runId)}>Open child: {child.agentName || child.runId} · {child.status}</button>)}
        {detail.data.childrenTruncated && <p className="text-xs mt-2" style={{ color: color.amber }}>Only the first 30 child runs are shown.</p>}
      </section>}
      {detail.data && !detail.data.parentRunId && !detail.data.children?.length && otherAgentRunsVisible && <p className="border p-3 mt-4 text-xs" style={{ borderColor: color.border, color: color.sub }}>Other captured agents on this page are unlinked to this run. A handoff cannot be inferred from agent names or timestamps; record a parent run ID to connect them.</p>}
      <EvidenceExplain root={url.replace(/\/api\/runs$/, "")} scope="run" runId={run.runId} />
      {["codex", "claude-code"].includes(run.sourceKind)
        && (run.eventCount >= 1500 || run.sourcePartial || run.evidenceCoverage?.sourcePartial)
        && <RecentLocalEvents url={url} runId={run.runId} recentReference={recentReference} onOpenRecentEvent={onOpenRecentEvent} autoAnalyze={Boolean(findingCode && !focusedFinding && run.sourcePartial)} />}
      <PrefixShell {...(localPartial ? { "aria-label": "Historical prefix details", className: "mt-5 border p-3", style: { borderColor: color.border } } : {})}>
      {localPartial && <summary className="cursor-pointer font-semibold">Historical prefix details · {run.eventCount} events</summary>}
      <section className="mt-5" aria-label="Run investigation">
        <h2 className="font-semibold">{focusedFinding ? `What happened: ${findingLabel(focusedFinding.code)}` : "What happened"}</h2>
        {findingCode && !focusedFinding && <p className="text-sm mt-2" style={{ color: color.amber }}>{run.sourcePartial ? "The quick-view prefix does not contain this finding. Verdict is checking the retained recorded-run evidence above." : "The selected finding is no longer present in this run. Other recorded findings are shown below."}</p>}
        <div className="mt-3 space-y-2">
          {findings.length ? findings.map((finding, index) => (
            <button key={`${finding.code}-${index}`} disabled={!finding.evidenceEventIds?.length} onClick={() => onFocusEvent(finding.evidenceEventIds[0])} className="border p-3 text-sm text-left w-full" style={{ borderColor: finding === focusedFinding ? color.amber : color.border }}>
              <span className="text-xs" style={{ color: finding.severity === "error" ? color.red : finding.severity === "warning" ? color.amber : color.sub }}>{findingLabel(finding.code)}</span>
              {finding.scope === "historical_prefix" && <span className="block text-xs mt-1" style={{ color: color.amber }}>Observed in captured historical prefix</span>}
              <div className="mt-1">{findingMessage(finding.message)}</div>
              <div className="text-xs mt-1" style={{ color: color.faint }}>{finding.evidenceEventIds?.length ? `Inspect ${finding.evidenceEventIds.length} linked evidence ${finding.evidenceEventIds.length === 1 ? "event" : "events"}` : "No event-level evidence is available for this finding."}</div>
            </button>
          )) : terminalFailure ? <div className="border p-3 text-sm" style={{ borderColor: color.amber }}>
            {run.sourcePartial ? "Recorded prefix execution status" : "Recorded execution status"}: {run.status}. This status alone does not establish whether the task succeeded or why the run ended.
          </div> : <div className="text-sm" style={{ color: color.sub }}>{run.sourcePartial ? "No deterministic findings in the captured historical prefix. Inspect the recent window for scoped checks." : "No deterministic findings in this run."}</div>}
        </div>
        <h3 className="font-semibold mt-5">Suggested next action</h3>
        <div className="mt-2 space-y-2">
          {linkedRecommendations.length ? linkedRecommendations.map((item) => <div key={item.code} className="border p-3 text-sm" style={{ borderColor: color.border }}>
            <div className="text-xs uppercase" style={{ color: color.amber }}>{item.status}</div>
            <div className="mt-1">{item.observation}</div>
            <div className="mt-1 font-semibold">{item.action}</div>
            <div className="text-xs mt-1" style={{ color: color.sub }}>{item.rationale} Tradeoff: {item.tradeoff}</div>
            {item.evidenceEventIds?.length > 0 && <button type="button" className="underline text-xs mt-2" onClick={() => onFocusEvent(item.evidenceEventIds[0])}>Inspect recorded evidence</button>}
          </div>) : focusedFinding?.code === "possible_tool_loop" ? <div className="border p-3 text-sm" style={{ borderColor: color.border }}>
            <div className="font-semibold">Inspect the repeated calls and their results before changing the workflow.</div>
            <p className="text-xs mt-1" style={{ color: color.sub }}>The same tool and arguments were recorded repeatedly within a turn. Review the linked events to see whether retries or repeated reads were necessary. If you change the workflow, compare it against the same task checks; this finding does not establish wasted calls or preserved quality.</p>
            {focusedFinding.evidenceEventIds?.length > 0 && <button type="button" className="underline text-xs mt-2" onClick={() => onFocusEvent(focusedFinding.evidenceEventIds[0])}>Inspect recorded evidence</button>}
          </div> : focusedFinding && failureRechecks[focusedFinding.code] && focusedFinding.evidenceEventIds?.length ? <div className="border p-3 text-sm" style={{ borderColor: color.border }}>
            <div className="font-semibold">{failureRechecks[focusedFinding.code][0]}</div>
            <p className="text-xs mt-1" style={{ color: color.sub }}>{failureRechecks[focusedFinding.code][1]} This finding records a failure, not its cause or recovery.</p>
            <button type="button" className="underline text-xs mt-2" onClick={() => onFocusEvent(focusedFinding.evidenceEventIds[0])}>Inspect recorded evidence</button>
          </div> : <div className="text-sm" style={{ color: color.sub }}>{findingCode && !focusedFinding && run.sourcePartial ? "The quick-view prefix cannot suggest an action for this issue. Use the recorded-run analysis above." : focusedFinding ? "No action is linked to this finding. Inspect its recorded evidence before deciding what to change." : terminalFailure ? "Inspect the run's recorded status and timeline; verify the task outcome with the application's own check before changing the workflow." : "No evidence-backed action suggested for this run."}</div>}
          {otherRecommendations.length > 0 && <details className="border p-3" style={{ borderColor: color.border }}><summary className="cursor-pointer text-sm">Other suggested actions ({otherRecommendations.length})</summary>
            <div className="mt-3 space-y-3">{otherRecommendations.map((item) => <div key={item.code} className="text-sm"><strong>{item.action}</strong><div className="text-xs mt-1" style={{ color: color.sub }}>{item.observation}</div></div>)}</div>
          </details>}
        </div>
        {focusEventId && <div className="mt-5" aria-label="Recorded evidence"><h3 className="font-semibold mb-2">Recorded evidence</h3>
          {linkedEvidenceIds.length > 1 && selectedEvidenceIndex >= 0 && <div className="flex items-center gap-3 text-xs mb-3">
            <button type="button" className="underline disabled:opacity-40" disabled={selectedEvidenceIndex === 0} onClick={() => onFocusEvent(linkedEvidenceIds[selectedEvidenceIndex - 1])}>Previous event</button>
            <span>Event {selectedEvidenceIndex + 1} of {linkedEvidenceIds.length}</span>
            <button type="button" className="underline disabled:opacity-40" disabled={selectedEvidenceIndex === linkedEvidenceIds.length - 1} onClick={() => onFocusEvent(linkedEvidenceIds[selectedEvidenceIndex + 1])}>Next event</button>
          </div>}
          {selectedEvent ? <EventRow event={selectedEvent} root={url.replace(/\/api\/runs$/, "")} runId={run.runId} focused onOpenTrace={onOpenTrace} />
            : <p role="status" className="text-sm" style={{ color: detail.loading ? color.sub : color.amber }}>{detail.loading ? "Loading linked evidence…" : "The selected evidence event is unavailable in this run."}</p>}
        </div>}
        {otherFindings.length > 0 && <details className="mt-5 border p-3" style={{ borderColor: color.border }}><summary className="cursor-pointer text-sm">Other recorded findings ({otherFindings.length})</summary>
          <div className="mt-3 space-y-2">{otherFindings.map((finding, index) => <button key={`${finding.code}-${index}`} type="button" disabled={!finding.evidenceEventIds?.length} onClick={() => onFocusEvent(finding.evidenceEventIds[0])} className="w-full border p-3 text-left text-sm" style={{ borderColor: color.border }}>{findingLabel(finding.code)} · {findingMessage(finding.message)}</button>)}</div>
        </details>}
      </section>
      </PrefixShell>
      <details className="mt-6 border p-4" style={{ borderColor: color.border }}>
        <summary className="cursor-pointer font-semibold">Technical coverage and further evaluation</summary>
      <div className="mt-5 grid sm:grid-cols-3 gap-2">
        {Object.entries(run.evidenceCoverage || {}).map(([name, value]) => (
          <div key={name} className="border p-3" style={{ borderColor: color.border }}>
            <div className="text-xs" style={{ color: color.faint }}>{name.replaceAll("_", " ")}</div>
            <div className="text-sm mt-1">{value.replaceAll("_", " ")}</div>
          </div>
        ))}
      </div>
      <div className="mt-4 grid sm:grid-cols-4 gap-2">
        <StatusFact label={run.sourcePartial ? "Recorded prefix outcome" : "Source outcome"} value={run.sourceOutcome || run.status} />
        <StatusFact label={run.sourcePartial ? "Recorded prefix turn outcomes" : "Turn outcomes"} value={displayCounts(run.turnOutcomes)} />
        <StatusFact label="Finding severity" value={displayCounts(run.findingSeverity)} />
        <StatusFact label="Selected Trace evaluator" value={run.evaluationCoverage?.state === "selected" ? `${run.evaluationCoverage.judged} judged · ${run.evaluationCoverage.notJudged} not judged · ${run.evaluationCoverage.judgeErrors} errors` : "Not selected"} />
      </div>
      <div className="mt-4 text-xs" style={{ color: color.sub }}>
        Estimated API list price: {sourceUsage.listPriceUsd == null ? "Not priced" : `$${Number(sourceUsage.listPriceUsd).toFixed(4)}`} ({sourceUsage.pricedTurns || 0}/{run.turnCount} turns priced). This is not a subscription bill; missing and mixed-model turns remain unpriced.
      </div>
      {run.scopedEvaluationEligible !== false && <ScopedEvaluate root={url.replace(/\/api\/runs$/, "")} scope="run" runId={run.runId} />}
      {run.scopedEvaluationEligible === false && <p className="mt-3 text-xs" style={{ color: color.sub }}>Whole-run evaluation unavailable: {run.scopedEvaluationUnavailableReason?.replaceAll("_", " ") || "incomplete evidence"}. Open a turn or tool action to inspect smaller evidence.</p>}
      {onDraftCase && <button type="button" className="border px-3 py-2 mt-3 text-xs" disabled={detail.loading || !detail.data?.turns?.length}
        onClick={() => {
          const turn = detail.data.turns.find((item) => typeof item.request === "string" && item.request.trim() && !item.requestTruncated);
          onDraftCase({ runId: run.runId, turnId: turn?.turnId || null, request: turn?.request || "", complete: Boolean(turn) });
        }}>Draft a reviewed case from this run…</button>}
      </details>
      <details className="mt-6" defaultOpen={!focusedFinding}>
        <summary className="cursor-pointer font-semibold">Turns and full event timeline</summary>
      <h2 className="font-semibold mt-6">Turns</h2>
      <div className="text-xs mt-1" style={{ color: color.sub }}>{turnEvaluatorFilter ? "Showing only this exact Turn evaluator." : `Showing the latest valid current Turn result among the ${detail.data?.turnEvaluationScope?.maxEvaluatorsPerTurn || 8} newest evaluator slots per Turn, if any. Older results require an exact evaluator fingerprint. Trace evaluator coverage above is separate.`}</div>
      <div className="flex flex-wrap gap-2 mt-2 items-center text-xs"><input aria-label="Exact Turn evaluator fingerprint" placeholder="Exact 64-character evaluator fingerprint" value={turnEvaluatorInput} onChange={(event) => onTurnEvaluatorInput(event.target.value)} className="border p-2 bg-transparent min-w-[200px] flex-1 font-mono" /><button className="border px-3 py-2" disabled={!/^[0-9a-f]{64}$/.test(turnEvaluatorInput)} onClick={() => onTurnEvaluatorFilter(turnEvaluatorInput)}>Show exact evaluator</button><button className="border px-3 py-2" disabled={!turnEvaluatorFilter} onClick={() => { onTurnEvaluatorInput(""); onTurnEvaluatorFilter(null); }}>Show latest</button></div>
      <div className="mt-2 space-y-2">
        {(detail.data?.turns || []).map((turn) => (
          <details key={turn.turnId} className="border p-3" style={{ borderColor: color.border }}>
            <summary className="text-sm cursor-pointer">Turn {turn.sequence + 1} · {turn.status}</summary>
            <div className="mt-3 text-xs" style={{ color: color.sub }}>
              <div>Request ({turn.requestState}{turn.requestTruncated ? ", bounded preview" : ""}): {turn.request ?? "not available"}</div>
              <div className="mt-2">Response ({turn.responseState}{turn.responseTruncated ? ", bounded preview" : ""}): {turn.response ?? "not available"}</div>
              <div className="mt-2">Source-reported token usage: {turnTokenSummary(turn.tokenUsage)}</div>
              <div className="mt-2">{turnEvaluatorFilter ? "Exact Turn evaluation" : "Latest valid current Turn evaluation (bounded)"}: {turn.evaluation ? `${turn.evaluation.status} · ${turn.evaluation.rubricName}/${turn.evaluation.rubricVersion} · ${turn.evaluation.judgeModels?.join(", ")} · ${turn.evaluation.toolEvidence === "counts_v1" ? "recorded tool counts included" : "tool counts not included in this judgment"} · evaluator ${turn.evaluation.evaluatorFingerprint} · ${turn.evaluation.dimensions.map((d) => `${d.name}: ${d.verdict}`).join(" · ") || "no scores"}` : "No current native Turn result for this scope"}</div>
              {turn.evaluation?.toolEvidence === "counts_v1" && turn.evaluation.toolCounts && <div className="text-xs mt-1" style={{ color: color.sub }}>Recorded tool events: {turn.evaluation.toolCounts.calls} calls · {turn.evaluation.toolCounts.results} results · {turn.evaluation.toolCounts.errorResults} reported errors · {turn.evaluation.toolCounts.unknownResults} unknown outcomes. Direct dispatch origins: {turn.evaluation.toolCounts.mcpCalls} MCP · {turn.evaluation.toolCounts.applicationCalls} application · {turn.evaluation.toolCounts.providerHostedCalls} provider-hosted · {turn.evaluation.toolCounts.originNotCapturedCalls} not captured · {turn.evaluation.toolCounts.originUnusableCalls} unusable. Origin labels describe only the producer-recorded outer dispatch boundary; they do not verify unobserved downstream services, protocols, sources, or claims.</div>}
            </div>
            <TurnAnalysis key={`${run.runId}:${turn.turnId}`} url={url} runId={run.runId} turnId={turn.turnId} onFocusEvent={onFocusEvent} />
            <EvidenceExplain root={url.replace(/\/api\/runs$/, "")} scope="turn" runId={run.runId} turnId={turn.turnId} />
            {onEvaluateTurn && <button type="button" className="border px-3 py-2 mt-2 text-xs" onClick={() => onEvaluateTurn(run.runId, turn.turnId)}>Evaluate this turn…</button>}
          </details>
        ))}
        {detail.data?.turnPage && <div className="flex items-center justify-between gap-2 text-xs" style={{ color: color.sub }}><span>Showing {detail.data.turnPage.shown ? detail.data.turnPage.offset + 1 : 0}-{detail.data.turnPage.offset + detail.data.turnPage.shown} of {detail.data.turnPage.available} turns.</span><span className="flex gap-2"><button disabled={detail.data.turnPage.offset === 0} onClick={() => onTurnPage(Math.max(0, detail.data.turnPage.offset - detail.data.turnPage.limit))} className="border px-3 py-1">Previous</button><button disabled={!detail.data.turnPage.truncated} onClick={() => onTurnPage(detail.data.turnPage.offset + detail.data.turnPage.limit)} className="border px-3 py-1">Next</button></span></div>}
      </div>
      <h2 className="font-semibold mt-6">Execution timeline</h2>
      {detail.data?.producerCount > 1 && <div className="text-xs mt-1" style={{ color: color.sub }}>Events are placed by timestamp. Sequence is authoritative only within each named producer.</div>}
      {detail.loading && <div className="text-sm mt-2" style={{ color: color.sub }}>Loading ordered evidence…</div>}
      {detail.error && <div role="alert" className="text-sm mt-2" style={{ color: color.red }}>{detail.error}</div>}
      {detail.data && focusEventId && !detail.data.events.some((event) => event.eventId === focusEventId)
        && <div role="status" className="text-sm mt-2" style={{ color: color.amber }}>The requested agent event is unavailable in this run.</div>}
      {detail.data && <div className="mt-2 space-y-2">
        {detail.data.events.map((event) => <EventRow key={event.eventId} root={url.replace(/\/api\/runs$/, "")} runId={run.runId} event={event} focused={event.eventId === (detail.data.focusEventId || focusEventId)} onOpenTrace={onOpenTrace} />)}
        {!detail.data.events.length && <div className="text-sm" style={{ color: color.sub }}>No normalized events were captured for this run.</div>}
        <div className="flex items-center justify-between gap-2 text-xs" style={{ color: color.sub }}><span>Showing {detail.data.page.shown ? detail.data.page.offset + 1 : 0}-{detail.data.page.offset + detail.data.page.shown} of {detail.data.page.available} events.</span><span className="flex gap-2"><button disabled={detail.data.page.offset === 0} onClick={() => onEventPage(Math.max(0, detail.data.page.offset - detail.data.page.limit))} className="border px-3 py-1">Previous</button><button disabled={!detail.data.page.truncated} onClick={() => onEventPage(detail.data.page.offset + detail.data.page.limit)} className="border px-3 py-1">Next</button></span></div>
      </div>}
      </details>
    </section>
  );
}

export function TurnAnalysis({ url, runId, turnId, onFocusEvent }) {
  const [view, setView] = useState({ open: false, loading: false, error: null, data: null });
  const request = React.useRef(null);
  useEffect(() => () => request.current?.abort(), [runId, turnId]);
  const load = () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setView((current) => ({ ...current, open: true, loading: true, error: null }));
    fetch(`${url}/${encodeURIComponent(runId)}/turns/${encodeURIComponent(turnId)}/analysis`, {
      credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((data) => {
        if (!controller.signal.aborted) setView((current) => ({ ...current, loading: false, error: null, data }));
      })
      .catch((error) => {
        if (!controller.signal.aborted) setView((current) => ({ ...current, loading: false, error: String(error), data: null }));
      });
  };
  const data = view.data;
  return <div className="mt-3 border-t pt-3 text-xs" style={{ borderColor: color.border }}>
    <div className="flex flex-wrap gap-2 items-center">
      <button className="border px-3 py-2" onClick={() => {
        if (!view.data && !view.loading) load();
        else setView((current) => ({ ...current, open: !current.open }));
      }}>{view.open ? "Hide analysis" : "Analyze turn"}</button>
      {view.open && data && <button className="underline" onClick={load}>Refresh analysis</button>}
      <span style={{ color: color.faint }}>On demand · local evidence review</span>
    </div>
    {view.open && <div className="mt-3 space-y-3">
      {view.loading && <div role="status" style={{ color: color.sub }}>Analyzing captured events…</div>}
      {view.error && <div role="alert" style={{ color: color.red }}>Turn analysis unavailable: {view.error}</div>}
      {data && <>
        <p style={{ color: color.sub }}>This shows what was recorded around the turn, with links to the source events. It cannot establish the agent’s private intent or whether the final answer was correct.</p>
        {!data.eventCoverage.complete && <p style={{ color: color.amber }}>{data.eventCoverage.sourcePartial ? "Source capture reported missing events." : `Partial event view: ${data.eventCoverage.shown} of ${data.eventCoverage.available} events.`} Later actions or outcomes may be missing.</p>}
        {data.eventCoverage.multipleProducers && <p style={{ color: color.amber }}>Several producers contributed events. Their timestamps do not establish one authoritative action order.</p>}
        <div>
          <div className="font-semibold" style={{ color: color.text }}>Observed actions</div>
          {data.actions.length ? <ol className="mt-1 space-y-1">{data.actions.map((action) => <li key={action.eventId}>
            <button className="underline text-left" onClick={() => onFocusEvent(action.eventId)}>{action.label}</button>
            {action.resultEventId && <> · <button className="underline" onClick={() => onFocusEvent(action.resultEventId)}>open matched result</button></>}
          </li>)}</ol> : <p style={{ color: color.sub }}>No tool, command, delegation, or retry action was captured.</p>}
          {data.actionsTruncated && <p style={{ color: color.amber }}>More actions are recorded than shown here.</p>}
        </div>
        <div>
          <div className="font-semibold" style={{ color: color.text }}>What to review</div>
          {data.decisionReview?.map((item) => <p key={item.eventId} className="mt-1" style={{ color: color.amber }}><button className="underline text-left" onClick={() => onFocusEvent(item.eventId)}>{item.label}</button></p>)}
          {data.reviewSignals.length ? <ul className="mt-1 space-y-1">{data.reviewSignals.map((signal) => <li key={signal.eventId}>
            <button className="underline text-left" onClick={() => onFocusEvent(signal.eventId)}>{signal.label}</button>
          </li>)}</ul> : <p style={{ color: color.sub }}>No failure signal in the events shown. This does not establish task success.</p>}
          {data.signalsTruncated && <p style={{ color: color.amber }}>More failure signals are recorded than shown here.</p>}
        </div>
      </>}
    </div>}
  </div>;
}

function displayCounts(values) {
  return Object.entries(values || {}).map(([name, count]) => `${name}: ${count}`).join(" · ") || "None";
}

function StatusFact({ label, value }) {
  return <div className="border p-3" style={{ borderColor: color.border }}><div className="text-xs" style={{ color: color.faint }}>{label}</div><div className="text-sm mt-1">{value}</div></div>;
}

function EventRow({ event, root, runId, focused = false, onOpenTrace = null, idPrefix = "" }) {
  const attributes = Object.entries(event.attributes || {});
  const failed = Boolean(event.issueCode) || event.status === "failed";
  return (
    <details id={`${idPrefix}event-${event.eventId}`} open={focused} className="border p-3" style={{ borderColor: failed ? color.red : focused ? color.amber : color.border }}>
      <summary className="text-sm cursor-pointer flex flex-wrap gap-2">
        <span className="font-mono" style={{ color: failed ? color.red : color.green }}>#{event.timelineIndex + 1}</span>
        <span>{event.type.replaceAll("_", " ")}</span>
        <span style={{ color: color.sub }}>{event.status}</span>
        {event.traceId && <button className="font-mono underline" style={{ color: color.amber }} onClick={(click) => { click.preventDefault(); click.stopPropagation(); onOpenTrace?.(event.traceId); }}>Trace {event.traceId}</button>}
        {event.judgment?.dimensions?.map((dimension) => <span key={dimension.name} className="font-mono" style={{ color: dimension.verdict === "pass" ? color.green : dimension.verdict === "fail" ? color.red : color.amber }}>{dimension.name}: {dimension.verdict}</span>)}
        <span className="ml-auto text-xs font-mono" style={{ color: color.faint }}>{event.occurredAt}</span>
      </summary>
      <div className="mt-3 grid gap-2 text-xs">
        <div style={{ color: color.sub }}>For a later-pass attachment in Monitoring, select these exact IDs:</div>
        <div className="font-mono break-all select-all">Run ID {runId}</div>
        <div className="font-mono break-all select-all">Event ID {event.eventId}</div>
        <div style={{ color: color.faint }}>Source: {event.provenance} · privacy: {event.privacy}</div>
        {event.producerId && <div style={{ color: color.faint }}>Producer: {event.producerId}{event.producerSequence == null ? "" : ` · sequence ${event.producerSequence}`}</div>}
        {event.omissionReason && <div style={{ color: color.amber }}>Omitted: {event.omissionReason}</div>}
        {attributes.map(([name, value]) => <div key={name} className="grid sm:grid-cols-[140px_minmax(0,1fr)] gap-2">
          <span className="font-mono" style={{ color: color.sub }}>{name}</span>
          <pre className="whitespace-pre-wrap break-words" style={{ color: color.text }}>{typeof value === "string" ? value : JSON.stringify(value)}</pre>
        </div>)}
        {event.type === "tool_call" && <ScopedEvaluate root={root} scope="tool_action" runId={runId} eventId={event.eventId} />}
        <EvidenceExplain root={root} scope="event" runId={runId} eventId={event.eventId} />
      </div>
    </details>
  );
}

function Notice({ icon: Icon, text }) {
  return <div className="border p-5 flex items-center gap-3 text-sm" style={{ borderColor: color.border, background: color.panel, color: color.sub }}><Icon size={16} />{text}</div>;
}
