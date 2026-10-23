import React, { useEffect, useState } from "react";
import { ScopedEvaluate } from "./ScopedEvaluate.jsx";
import { EvidenceExplain } from "./EvidenceExplain.jsx";

const C = { panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f", green: "#4ee1aa", bar: "#38b993", amber: "#f2b84b", red: "#ff6b6b" };

const count = (value) => value == null ? "Not captured" : Number(value).toLocaleString();
const price = (value) => value == null ? "Not priced" : `$${Number(value).toFixed(value > 0 && value < 0.0001 ? 8 : value > 0 && value < 0.01 ? 6 : 2)}`;
const coverage = (known, total) => `${known}/${total} turns`;
const component = (session, name, label) => `${count(session[name])} ${label} (${coverage(session.componentCoverage?.[name] || 0, session.turnCount)})`;

export function UsageChart({ days, selectedDay, measure, onSelectDay, compact = false, issueOnly = false }) {
  const scrollRef = React.useRef(null);
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollLeft = scrollRef.current.scrollWidth;
  }, []);
  const metric = measure === "price" ? "listPriceUsd" : "totalTokens";
  const maximum = Math.max(1, ...days.map((day) => day[metric] || 0));
  const tickIndices = days.length ? [...new Set([0, 0.25, 0.5, 0.75, 1]
    .map((fraction) => Math.round((days.length - 1) * fraction)))] : [];
  const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dateLabel = (day) => {
    const month = Number(day.slice(5, 7));
    const date = Number(day.slice(8, 10));
    return monthNames[month - 1] && date >= 1 && date <= 31 ? `${monthNames[month - 1]} ${date}` : day;
  };
  return <div ref={scrollRef} className="min-w-0 overflow-x-auto mt-4">
    <div className={`w-full ${compact ? "min-w-[300px]" : "min-w-[650px]"}`}>
    <div className="flex gap-1 h-32 items-end border-b" style={{ borderColor: C.border }} role="group" aria-label={issueOnly ? "Recorded issue events by UTC day" : "Daily agent usage in UTC"}>
      {days.map((day) => {
        const value = day[metric];
        const height = value == null ? 0 : Math.max(value > 0 ? 4 : 0, value / maximum * 118);
        const usageLabel = day.turns === 0 ? `${day.day} UTC: no captured turns`
          : measure === "price"
            ? `${day.day} UTC: ${day.pricedTurns ? price(value) : "list price unavailable"}; ${coverage(day.pricedTurns, day.turns)} priced`
            : `${day.day} UTC: ${day.tokenKnownTurns ? `${count(value)} reported tokens` : "token totals unavailable"}; ${coverage(day.tokenKnownTurns, day.turns)} with totals`;
        const issueLabel = day.recordedIssueEvents == null ? "; recorded issue scan incomplete"
          : day.recordedIssueEvents > 0 ? `; ${day.issueSourcePartial ? "at least " : ""}${day.recordedIssueEvents} recorded issue event${day.recordedIssueEvents === 1 ? "" : "s"}` : "";
        const label = `${usageLabel}${day.sourcePartial ? "; local turn coverage incomplete or unverified" : ""}${day.issueSourcePartial ? "; local issue-event coverage incomplete or unverified" : ""}${issueLabel}`;
        const className = `flex-1 ${compact ? "min-w-[6px]" : "min-w-[16px]"} h-full flex flex-col justify-end items-center`;
        const style = { color: selectedDay === day.day ? C.green : C.bar };
        const bar = <span className="w-full rounded-t-sm" style={{ height, background: selectedDay === day.day ? C.green : C.bar }} />;
        const marker = day.recordedIssueEvents > 0 && <span aria-hidden="true" className="w-2 h-2 rounded-full mb-1" style={{ background: C.red }} />;
        return day.turns === 0 && !day.recordedIssueEvents && !day.sourcePartial && !day.issueSourcePartial
          ? <span key={day.day} role="img" aria-label={label} title={label} className={className} style={style}>{bar}</span>
          : <button key={day.day} type="button" aria-label={label} aria-pressed={selectedDay === day.day}
            title={label} onClick={() => onSelectDay(day.day)} className={className} style={style}>{marker}{bar}</button>;
      })}
    </div>
    <div className="flex justify-between gap-2 mt-2 text-[11px]" style={{ color: C.sub }} aria-label="UTC date axis">
      {tickIndices.map((index) => <span key={days[index].day} className="whitespace-nowrap">{dateLabel(days[index].day)}</span>)}
    </div>
    </div>
  </div>;
}

export function DayIssueSummary({ day, issues, onOpenRun }) {
  if (!issues) return null;
  const partialSource = !issues.complete && issues.sourcePartial && Number.isInteger(issues.observedCount);
  return <section className="border p-3 mt-4 text-sm" style={{ borderColor: C.border }} aria-label="Recorded issue events on selected UTC day">
    <h3 className="font-semibold">Recorded issues on {day} UTC</h3>
    {!issues.complete && !partialSource ? <p className="mt-2" style={{ color: C.amber }}>The bounded event scan is incomplete; no issue count is shown.</p>
      : <><p className="mt-2">{partialSource
        ? issues.observedCount > 0
          ? `At least ${issues.observedCount} recorded issue event${issues.observedCount === 1 ? "" : "s"} in captured evidence; source history is partial, so the full-day count is unknown.`
          : "No recorded issue events in captured evidence; source history is partial, so the full-day count is unknown."
        : `${issues.count} event${issues.count === 1 ? "" : "s"} matched the recorded issue checks on this date. This is based on event time, not when the issue was processed or when a run started.`}</p>
        {issues.events.length > 0 && <div className="flex flex-wrap gap-3 mt-2">{issues.events.map((item, index) => <button key={`${item.runId}:${item.eventId}`} type="button" className="underline" style={{ color: C.red }} onClick={() => onOpenRun?.(item.runId, item.eventId, item.recentRevision, item.recentTurnId)}>Inspect {item.code.replaceAll("_", " ")} {index + 1}</button>)}</div>}
        {issues.eventsTruncated && <p className="mt-2" style={{ color: C.amber }}>Showing the first {issues.events.length} recorded events; open Investigation for more.</p>}
        <p className="mt-2" style={{ color: C.sub }}>These counts do not establish task success. Late capture may update an earlier day.</p></>}
  </section>;
}

function ModelCallTrend({ report, onOpenCalls }) {
  const rows = report?.timeline?.rows || [];
  const periodDays = report?.period?.days;
  const periodLabel = periodDays === 0 ? "all captured dates"
    : Number.isInteger(periodDays) && periodDays > 0 ? `last ${periodDays} UTC days`
      : "selected report period";
  const known = rows.filter((row) => row.tokenKnownCalls > 0);
  const [selectedDate, setSelectedDate] = useState(null);
  const selected = rows.find((row) => row.date === selectedDate) || known.at(-1);
  if (!known.length) return null;
  const maximum = Math.max(1, ...known.map((row) => row.totalTokens || 0));
  return <div className="mt-2" role="region" aria-label="Daily model-call token usage">
    <div className="text-sm font-semibold">Recorded model-call tokens by UTC day · {periodLabel}</div>
    {report?.timeline?.availableDates > rows.length && <p className="text-xs mt-1" style={{ color: C.sub }}>Showing the newest {rows.length} of {report.timeline.availableDates} active dates in this period.</p>}
    <p className="text-xs mt-1" style={{ color: C.sub }}>Provider response records have token totals even when agent turns do not. Missing call totals are excluded.</p>
    <div className="flex gap-1 items-end h-28 mt-3 border-b overflow-x-auto" style={{ borderColor: C.border }}>
      {rows.map((row) => <button key={row.date} type="button"
        aria-label={`${row.date} UTC: ${row.tokenKnownCalls ? count(row.totalTokens) : "unknown"} model-call tokens; ${row.tokenKnownCalls}/${row.calls} calls with totals`}
        aria-pressed={selected?.date === row.date}
        onClick={() => setSelectedDate(row.date)}
        className="flex-none h-full flex flex-col justify-end"
        style={{ width: Math.max(8, Math.min(40, Math.floor(660 / rows.length))) }}
        title={`${row.date}: ${row.tokenKnownCalls ? count(row.totalTokens) : "unknown"} tokens (${row.tokenKnownCalls}/${row.calls} calls)`}>
        <span className="w-full rounded-t-sm" style={{ height: row.tokenKnownCalls ? `${Math.max(4, (row.totalTokens || 0) / maximum * 100)}%` : 0,
          background: selected?.date === row.date ? C.green : C.bar }} />
      </button>)}
    </div>
    <div className="flex justify-between text-[11px] mt-2" style={{ color: C.sub }}><span>{rows[0]?.date}</span>{rows.length > 1 && <span>{rows.at(-1)?.date}</span>}</div>
    {selected && <div className="text-sm mt-3">{selected.date} UTC · {selected.tokenKnownCalls ? `${count(selected.totalTokens)} recorded model-call tokens` : "Token total unavailable"} · {selected.tokenKnownCalls}/{selected.calls} calls with totals</div>}
    {onOpenCalls && <button type="button" className="text-sm underline mt-2" style={{ color: C.green }}
      onClick={() => onOpenCalls(selected.date)}>Inspect model calls on {selected.date} UTC</button>}
  </div>;
}

function SessionExplanation({ session, selectedDay, dayTotal }) {
  const largest = session.topTurns?.find((turn) => turn.totalTokens != null);
  const largestShare = largest && session.totalTokens > 0
    ? Math.round(largest.totalTokens / session.totalTokens * 100) : null;
  const dayShare = session.selectedDayTokens != null && dayTotal > 0
    ? Math.round(session.selectedDayTokens / dayTotal * 100) : null;
  return <div role="region" aria-label="Session usage analysis" className="border p-4 mt-3 text-sm space-y-2" style={{ borderColor: C.border }}>
    <div className="font-semibold">What drove this session's reported usage</div>
    {largest ? <p>Turn {largest.sequence + 1} used {count(largest.totalTokens)} reported tokens{largestShare == null ? "" : `, ${largestShare}% of this session's known total`}. Open its run below to inspect the captured evidence.</p>
      : <p>No turn has a complete reported token total, so a usage driver cannot be identified.</p>}
    {dayShare != null && <p>This session accounted for {dayShare}% of reported tokens on {selectedDay} UTC. A session spanning days can also appear on another date.</p>}
    {(session.inputTokens != null || session.outputTokens != null) && <p>Recorded components: {component(session, "inputTokens", "input")}, {component(session, "outputTokens", "output")}, {component(session, "cachedInputTokens", "cache-read input")}, {component(session, "cacheWriteInputTokens", "cache-write input")}. Cached input may be included in input tokens, depending on the source; components should not be added together.</p>}
    <p>Token totals cover {coverage(session.tokenKnownTurns, session.turnCount)}. Estimated list price covers {coverage(session.pricedTurns, session.turnCount)}.</p>
    {session.elapsedSpanSeconds != null && <p>Observed span from first turn start to last turn end: {count(session.elapsedSpanSeconds)} seconds. This includes pauses and tool work; it is not model latency.</p>}
    <p style={{ color: C.sub }}>These are measured contributors, not a judgment of task quality or whether the tokens were necessary.</p>
  </div>;
}

export function DayAiExplanation({ root, day }) {
  const [provider, setProvider] = useState("anthropic");
  const [access, setAccess] = useState(null);
  const [environment, setEnvironment] = useState(null);
  const [preview, setPreview] = useState(null);
  const [approved, setApproved] = useState(false);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    let active = true;
    Promise.all([
      fetch(`${root}/api/setup/token`, { credentials: "same-origin" }).then((response) => response.json()),
      fetch(`${root}/api/evaluators`, { credentials: "same-origin" }).then((response) => response.json()),
    ]).then(([token, env]) => { if (active) { setAccess(token.setupToken); setEnvironment(env); } })
      .catch((failure) => { if (active) setError(String(failure)); });
    return () => { active = false; };
  }, [root]);
  const configured = environment?.providers?.find((item) => item.provider === provider)?.configured;
  const post = async (path, body) => {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": access },
        body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(false); }
  };
  return <div className="border p-3 mt-4 text-sm" style={{ borderColor: C.border }}>
    <div className="font-semibold">Optional AI interpretation</div>
    <p className="mt-1" style={{ color: C.sub }}>Ask why this day used these reported tokens. The preview contains aggregate metrics and numbered contributors, without chat text or tool outputs. The answer is an interpretation; inspect linked runs before changing a workflow.</p>
    <div className="flex flex-wrap items-end gap-3 mt-3">
      <label>Provider<select className="block border p-2 mt-1 bg-transparent" value={provider} disabled={busy} onChange={(event) => { if (busy) return; setProvider(event.target.value); setPreview(null); setApproved(false); setResult(null); }}><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option></select></label>
      <button type="button" className="border px-3 py-2" disabled={!access || busy} onClick={async () => { const data = await post("/api/usage/explain/preview", { day, provider }); if (data) { setPreview(data); setApproved(false); setResult(null); } }}>Preview AI context</button>
    </div>
    {preview && <div className="border p-3 mt-3" style={{ borderColor: C.border }}>
      <div>{preview.plannedCalls === 1 ? "One optional call" : "No usable token total for an AI explanation"} · {preview.provider}/{preview.model}</div>
      <div style={{ color: C.sub }}>Destination: {preview.destination}. Static list-price bound: {price(preview.upperStaticListPriceUsd)}; actual billing may differ.</div>
      <details className="mt-2"><summary className="cursor-pointer">Review exact messages and metrics</summary><pre className="whitespace-pre-wrap break-words max-h-72 overflow-auto mt-2 text-xs">{JSON.stringify(preview.messages, null, 2)}</pre></details>
      <label className="flex gap-2 mt-3"><input type="checkbox" checked={approved} disabled={preview.plannedCalls !== 1} onChange={(event) => setApproved(event.target.checked)} />I approve sending these exact messages to {preview.destination} once.</label>
      <button type="button" className="border px-3 py-2 mt-2" disabled={!approved || !configured || !preview.providerAvailable || busy || preview.plannedCalls !== 1} onClick={async () => { const data = await post("/api/usage/explain/run", { day, provider, planFingerprint: preview.planFingerprint, confirmExternalEgress: true }); if (data) setResult(data); }}>Ask AI why</button>
      {!configured && <p style={{ color: C.amber }}>Provider key is not configured for this dashboard process.</p>}
      {!preview.providerAvailable && <p style={{ color: C.amber }}>This provider needs the Verdict eval package and its SDK installed in the dashboard environment.</p>}
    </div>}
    {result && <div className="mt-3 border p-3" style={{ borderColor: C.border }} role="status">
      {result.status === "completed" ? <><div className="font-semibold">AI interpretation</div><p className="mt-2 whitespace-pre-wrap">{result.answer}</p></>
        : <p style={{ color: C.amber }}>The call has no available answer ({result.error || result.status}). This approved attempt will not run again automatically.</p>}
    </div>}
    {error && <p role="alert" className="mt-2" style={{ color: C.red }}>{error}</p>}
  </div>;
}

export function DayExplanation({ explanation, url, onSelectSession, onOpenRun }) {
  const comparison = explanation.comparison;
  return <div className="border p-4 mt-4 text-sm" style={{ borderColor: C.border }} role="region" aria-label="Day usage explanation">
    <div className="font-semibold">What drove {explanation.day} UTC</div>
    <p className="mt-2">{explanation.totalTokens == null
      ? "No captured turn on this day reported a usable token total."
      : `${count(explanation.totalTokens)} reported tokens across ${coverage(explanation.tokenKnownTurns, explanation.turns)} with totals.`}</p>
    <p className="mt-1" style={{ color: C.sub }}>{comparison && comparison.differencePercent != null
      ? `${count(Math.abs(comparison.differenceTokens))} ${comparison.differenceTokens >= 0 ? "above" : "below"} the median of ${comparison.referenceDays} prior active days (${comparison.medianTokens.toLocaleString()} tokens); ${Math.abs(comparison.differencePercent)}% ${comparison.differenceTokens >= 0 ? "higher" : "lower"}.`
      : "No reliable prior-day comparison is available from this bounded window."}</p>
    {(explanation.sourcePartial || explanation.referenceSourcePartial) && <p className="mt-1" style={{ color: C.amber }}>
      {explanation.sourcePartial ? "The selected day's local-history capture is incomplete or unverified. " : ""}
      {explanation.referenceSourcePartial ? "A prior comparison day's local-history capture is incomplete or unverified. " : ""}
      Reported totals are shown, but the day comparison waits for a complete verified import.
    </p>}
    {explanation.topSessions.length > 0 && <div className="mt-4"><div className="font-semibold">Largest sessions on this day</div>
      {explanation.topSessions.map((item, index) => <div key={`${item.sourceKind}:${item.sessionId}`} className="flex flex-wrap justify-between gap-2 border-b py-2" style={{ borderColor: C.border }}>
        <span>Session {index + 1} · {item.sourceKind} · {count(item.dayTokens)} tokens{item.sharePercent == null ? "" : ` · ${item.sharePercent}% of known day total`} ({coverage(item.tokenKnownTurns, item.turns)} with totals)</span>
        <button type="button" className="underline" style={{ color: C.green }} onClick={() => onSelectSession(item)}>Inspect session</button>
      </div>)}</div>}
    {explanation.topTurns.length > 0 && <div className="mt-4"><div className="font-semibold">Largest turns on this day</div>
      {explanation.topTurns.map((item, index) => <div key={`${item.runId}:${item.turnId}`} className="flex flex-wrap justify-between gap-2 border-b py-2" style={{ borderColor: C.border }}>
        <span>Turn {index + 1} · {count(item.totalTokens)} tokens{item.sharePercent == null ? "" : ` · ${item.sharePercent}% of known day total`} · {item.models.join(", ") || "model unavailable"}</span>
        <button type="button" className="underline" style={{ color: C.green }} onClick={() => onOpenRun?.(item.runId)}>Open run</button>
      </div>)}</div>}
    <p className="mt-3" style={{ color: C.sub }}>These are measured contributors. The day assigns each turn by its start time; a session report may include turns from other days. Token components can overlap, and observed elapsed time includes pauses and tool work.</p>
    {!explanation.windowComplete && <p style={{ color: C.amber }}>The 30-day scan is partial; older turns may be omitted.</p>}
    <DayAiExplanation root={url.replace(/\/api\/usage$/, "")} day={explanation.day} />
  </div>;
}

function SessionReport({ root, sessionId, sourceKind, onOpenRun }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: null, data: null });
    fetch(`${root}/api/session?${new URLSearchParams({ source_kind: sourceKind, session_id: sessionId })}`,
      { credentials: "same-origin", signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((data) => { if (!controller.signal.aborted) setState({ loading: false, error: null, data }); })
      .catch((error) => { if (!controller.signal.aborted && error.name !== "AbortError") setState({ loading: false, error: String(error), data: null }); });
    return () => controller.abort();
  }, [root, sessionId, sourceKind]);
  const report = state.data;
  return <div className="mt-4 border p-4 text-sm space-y-3" style={{ borderColor: C.border }}>
    <div className="font-semibold">Session report</div>
    {state.loading && <p role="status" style={{ color: C.sub }}>Loading session evidence…</p>}
    {state.error && <p role="alert" style={{ color: C.red }}>Session report unavailable: {state.error}</p>}
    {report && <>
      {report.coverage !== "complete" && <p role="status" style={{ color: C.amber }}>Partial report: {report.unavailableReason?.replaceAll("_", " ")}. Showing up to 20 turns and 80 event headers from the displayed runs. Open individual runs for more evidence; full-session judging and findings are unavailable.</p>}
      <p style={{ color: C.sub }}>{report.runCountAtLeast > report.runIds.length ? "More than " : ""}{report.runIds.length} recorded runs · {report.turnCount} turns · {report.eventCount} events. {report.usage ? `${count(report.usage.totalTokens)} reported tokens across ${coverage(report.usage.tokenKnownTurns, report.usage.turnCount)}${report.coverage === "complete" ? `; ${price(report.usage.listPriceUsd)} estimated list price across ${coverage(report.usage.pricedTurns, report.usage.turnCount)}` : " in the displayed run slice; list price withheld for this partial report"}.` : "Combined usage unavailable at this report size."}</p>
      <div className="flex flex-wrap gap-2">{report.runIds.map((runId) => <button type="button" className="underline" key={runId} style={{ color: C.green }} onClick={() => onOpenRun?.(runId)}>Open {runId}</button>)}</div>
      {report.findings?.length > 0 && <div><div className="font-semibold">Recorded findings</div>{report.findings.map((item, index) => <p className="mt-1" key={`${item.runId}:${item.code}:${index}`}>{item.runId}: {item.message}</p>)}</div>}
      {report.recommendations?.length > 0 && <div><div className="font-semibold">Suggested actions</div>{report.recommendations.map((item, index) => <p className="mt-1" key={`${item.runId}:${item.code}:${index}`}>{item.action} <span style={{ color: C.sub }}>{item.observation}</span></p>)}</div>}
      {report.evaluations?.length > 0 && <div><div className="font-semibold">Saved historical evaluations</div><p className="text-xs" style={{ color: C.sub }}>A saved grade may reflect earlier evidence; preview again before drawing a current conclusion.</p>{report.evaluations.map((item) => <p key={item.resultId}>{item.scope} · {item.status} · {item.dimensions?.map((dimension) => `${dimension.name}: ${dimension.verdict}${dimension.citationStatus === "linked" ? "" : " (citation unverified)"}`).join(", ") || "no scored dimensions"}</p>)}</div>}
      {report.turns?.length > 0 && <div><div className="font-semibold">Requests and outcomes</div>{report.turns.map((turn) => <details key={`${turn.runId}:${turn.turnId}`} className="border p-2 mt-2" style={{ borderColor: C.border }}><summary>{turn.startedAt} · {turn.runId} · turn {turn.sequence + 1} · {turn.status} · {count(turn.totalTokens)} tokens</summary><p className="mt-2 whitespace-pre-wrap break-words">Request: {turn.request || "Not captured"}</p><p className="mt-2 whitespace-pre-wrap break-words">Response: {turn.response || "Not captured"}</p></details>)}</div>}
      {report.events?.length > 0 && <details><summary>Recorded event timeline ({report.events.length}{report.coverage === "complete" ? "" : " shown; details in run"})</summary>{report.events.map((event) => <div key={`${event.runId}:${event.eventId}`} className="border p-2 mt-2" style={{ borderColor: C.border }}><span>{event.occurredAt} · {event.type} · {event.status} · {event.runId}</span>{report.coverage === "complete" && <pre className="whitespace-pre-wrap break-words text-xs mt-1">{JSON.stringify(event.attributes, null, 2)}</pre>}</div>)}</details>}
      {report.coverage === "complete" && report.scopedEvaluationEligible !== false && <ScopedEvaluate key={`${sourceKind}:${sessionId}`} root={root} scope="session" sessionId={sessionId} sourceKind={sourceKind} />}
      <EvidenceExplain root={root} scope="session" sessionId={sessionId} sourceKind={sourceKind} />
      {report.scopedEvaluationEligible === false && <p className="text-xs" style={{ color: C.sub }}>Whole-session evaluation unavailable: {report.scopedEvaluationUnavailableReason?.replaceAll("_", " ") || "incomplete evidence"}. Open a run to inspect smaller evidence.</p>}
    </>}
  </div>;
}

export function Usage({ url, onOpenRun, compact = false, home = false, modelCallReport = null, onOpenCalls = null, onData = null }) {
  const [day, setDay] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [sourceKind, setSourceKind] = useState(null);
  const [measure, setMeasure] = useState("tokens");
  const [analyzing, setAnalyzing] = useState(false);
  const [explainingDay, setExplainingDay] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const selectedSessionRef = React.useRef(null);
  const pendingSessionRef = React.useRef(null);
  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({ ...current, loading: true, error: null }));
    const query = new URLSearchParams();
    if (day) query.set("day", day);
    if (sessionId) query.set("session_id", sessionId);
    if (sourceKind) query.set("source_kind", sourceKind);
    const address = `${url}${query.size ? `?${query}` : ""}`;
    fetch(address, { credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((data) => { if (!controller.signal.aborted) { setState({ loading: false, error: null, data }); onData?.(url, data); } })
      .catch((error) => { if (!controller.signal.aborted && error?.name !== "AbortError") { setState({ loading: false, error: String(error), data: null }); onData?.(url, null); } });
    return () => controller.abort();
  }, [url, day, sessionId, sourceKind]);

  const data = state.data;
  const chosenDay = data?.days?.find((item) => item.day === data.selectedDay);
  const session = data?.selectedSession;
  useEffect(() => {
    const pending = pendingSessionRef.current;
    if (!pending || state.loading) return;
    if (state.error || !session) {
      pendingSessionRef.current = null;
      return;
    }
    if (session.sessionId !== pending.sessionId
        || session.sourceKind !== pending.sourceKind) return;
    pendingSessionRef.current = null;
    selectedSessionRef.current?.focus();
    selectedSessionRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [state.loading, state.error, session]);
  const inspectSession = (item) => {
    pendingSessionRef.current = item;
    setDay(data?.selectedDay || null);
    setSessionId(item.sessionId);
    setSourceKind(item.sourceKind);
    setAnalyzing(false);
    setReportOpen(false);
    if (session?.sessionId === item.sessionId && session?.sourceKind === item.sourceKind) {
      selectedSessionRef.current?.focus();
      selectedSessionRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
      pendingSessionRef.current = null;
    }
  };
  const hasActivity = data?.days?.some((item) => item.turns > 0);
  const hasTokenTotals = data?.days?.some((item) => item.tokenKnownTurns > 0);
  const hasPricedTurns = data?.days?.some((item) => item.pricedTurns > 0);
  const hasRecordedIssues = data?.days?.some((item) => item.recordedIssueEvents > 0);
  const latestIssueDay = data?.days?.slice().reverse().find((item) => item.recordedIssueEvents > 0);
  const showDayChart = hasTokenTotals || (!compact && (hasRecordedIssues || data?.selectedDayIssues?.complete === false));
  const showModelCallTrend = !hasTokenTotals && modelCallReport?.timeline?.rows?.some((row) => row.tokenKnownCalls > 0);
  const issueOnly = !hasTokenTotals && !showModelCallTrend && hasRecordedIssues;
  const issueOnlyChart = !hasTokenTotals && hasRecordedIssues;
  const selectedDayReady = day == null || data?.selectedDay === day;
  return <section className="min-w-0 border p-5" style={{ borderColor: C.border, background: C.panel }}>
    <div className="flex flex-wrap justify-between items-start gap-3">
      {!home && <>
      <div><div className="text-xs font-mono" style={{ color: C.green }}>{issueOnly ? "RECORDED ISSUES" : showModelCallTrend ? "TOKEN USAGE" : "LAST 30 UTC DAYS"}</div>
        <h2 className="text-lg font-semibold mt-1">{issueOnly ? "Recorded issue events" : "Where tokens went"}</h2>
        <p className="text-sm mt-2" style={{ color: C.sub }}>{issueOnly ? "Choose an issue date to inspect its recorded events." : showModelCallTrend ? "Select a day to inspect recorded model-call usage and coverage." : hasActivity && !hasTokenTotals ? "Inspect sessions with missing token totals." : "Choose a day to explain its token use, then inspect the sessions and turns behind it."}</p></div>
      </>}
      {!home && hasTokenTotals && !showModelCallTrend && <div className="flex gap-2 text-xs">
        <button type="button" aria-pressed={measure === "tokens"} onClick={() => setMeasure("tokens")} className="border px-3 py-2" style={{ borderColor: measure === "tokens" ? C.green : C.border }}>Tokens</button>
        <button type="button" aria-pressed={measure === "price"} disabled={data && !hasPricedTurns} onClick={() => setMeasure("price")} className="border px-3 py-2" style={{ borderColor: measure === "price" ? C.green : C.border, opacity: data && !hasPricedTurns ? 0.5 : 1 }}>Est. list price</button>
      </div>}
    </div>
    <div className="mt-4 text-sm" style={{ minHeight: state.loading || state.error ? 20 : 0 }} aria-live="polite">
      {state.loading && <span role="status" style={{ color: C.sub }}>Loading usage…</span>}
      {state.error && <span role="alert" style={{ color: C.red }}>Usage unavailable: {state.error}</span>}
    </div>
    {data && <>
      {!data.window.complete && <div role="status" className="mt-4 text-sm" style={{ color: C.amber }}>
        This 30-day view is bounded and partial.
        {data.window.turnsComplete === false && " Older turns may be omitted."}
        {data.window.modelEvidenceComplete === false && " Model attribution is incomplete for older turns; fully scanned newer turns retain list-price estimates."}
        {data.days.some((item) => item.sourcePartial) && " Local turn history is incomplete or unverified; recorded token totals may omit source activity."}
        {data.days.some((item) => item.issueSourcePartial) && " Local issue-event history is incomplete or unverified; issue counts are lower bounds."}
      </div>}
      {showModelCallTrend && <ModelCallTrend report={modelCallReport} onOpenCalls={onOpenCalls} />}
      {!hasActivity && !showModelCallTrend && <p className="mt-5 text-sm" style={{ color: C.sub }}>No agent turns were captured in this 30-day window.</p>}
      {hasActivity && !hasTokenTotals && <p className="mt-3 text-sm" style={{ color: C.sub }}>Agent turns did not report usable token totals. Session-level token attribution is unavailable.</p>}
      {compact && hasActivity && !hasTokenTotals && day == null && <button type="button" className="text-sm underline mt-3" style={{ color: C.green }} onClick={() => setDay(data.selectedDay)}>Inspect sessions with missing totals</button>}
      {compact && !hasTokenTotals && latestIssueDay && day == null && <button type="button" className="block text-sm underline mt-3" style={{ color: C.green }} onClick={() => setDay(latestIssueDay.day)}>Inspect recorded issue events on {latestIssueDay.day} UTC →</button>}
      {compact && !hasTokenTotals && data.selectedDayIssues?.complete === false && <p className="mt-3 text-sm" style={{ color: C.amber }}>{data.selectedDayIssues.sourcePartial ? "Local history is partial; recorded issues are a lower bound, not a full-day total." : "The recorded issue scan is incomplete; no issue total is assumed."}</p>}
      {compact && !hasTokenTotals && !hasRecordedIssues && data.selectedDayIssues?.sourcePartial && day == null && <button type="button" className="block text-sm underline mt-3" style={{ color: C.green }} onClick={() => setDay(data.selectedDay)}>Inspect incomplete day {data.selectedDay} UTC →</button>}
      {showDayChart && <>
        <div className="flex justify-between text-xs mt-4" style={{ color: C.sub }}><span>{!hasTokenTotals ? hasRecordedIssues ? "Recorded issue events by day" : "Issue scan incomplete" : measure === "price" ? "Estimated API list price per day" : "Reported tokens per day"}</span>{hasTokenTotals && <span>Peak {measure === "price" ? price(Math.max(...data.days.map((item) => item.listPriceUsd || 0))) : count(Math.max(...data.days.map((item) => item.totalTokens || 0)))}</span>}</div>
        <UsageChart days={data.days} selectedDay={data.selectedDay} measure={measure} compact={compact} issueOnly={issueOnlyChart} onSelectDay={(value) => { pendingSessionRef.current = null; setDay(value); setSessionId(null); setSourceKind(null); setAnalyzing(false); setReportOpen(false); setExplainingDay(false); }} />
      </>}
      {hasTokenTotals && data.days.every((item) => item.totalTokens === 0) && <p className="mt-2 text-sm" style={{ color: C.sub }}>Captured turns reported zero total tokens in this window.</p>}
      {compact && day == null && hasTokenTotals && <p className="mt-3 text-sm" style={{ color: C.sub }}>Latest active day: {data.selectedDay} UTC · {chosenDay?.tokenKnownTurns ? `${count(chosenDay.totalTokens)} reported tokens` : "token totals unavailable"} across {coverage(chosenDay?.tokenKnownTurns || 0, chosenDay?.turns || 0)} with totals. Click a bar to inspect sessions.</p>}
      {(!compact || day != null) && selectedDayReady && <>
      <div className="mt-5 grid sm:grid-cols-3 gap-2 text-sm">
        <Fact label={`${data.selectedDay} UTC`} value={chosenDay?.tokenKnownTurns ? `${count(chosenDay.totalTokens)} reported tokens` : chosenDay?.turns ? "Token totals unavailable" : "No captured token report"} detail={`${coverage(chosenDay?.tokenKnownTurns || 0, chosenDay?.turns || 0)} with totals`} />
        <Fact label="Estimated list price" value={chosenDay?.pricedTurns ? price(chosenDay.listPriceUsd) : "Not priced"} detail={`${coverage(chosenDay?.pricedTurns || 0, chosenDay?.turns || 0)} priced; not a subscription bill`} />
        <Fact label="Tokens without one recorded model" value={chosenDay?.tokenKnownTurns ? count(chosenDay.unattributedTokens) : "Not captured"} detail="Reported turn totals with no model name or multiple model names" />
      </div>
      <DayIssueSummary day={data.selectedDay} issues={data.selectedDayIssues} onOpenRun={onOpenRun} />
      {data.dayExplanation && <><button type="button" className="border px-3 py-2 mt-4 text-sm" style={{ borderColor: C.green, color: C.green }} onClick={() => setExplainingDay((value) => !value)}>{explainingDay ? "Hide day explanation" : "Explain this day"}</button>
        {explainingDay && <DayExplanation key={data.selectedDay} explanation={data.dayExplanation} url={url}
          onSelectSession={inspectSession} onOpenRun={onOpenRun} />}</>}
      {data.dayModels?.length > 0 && <div className="mt-5">
        <div className="font-semibold text-sm">Models on this day</div>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">{data.dayModels.map((item) => <span key={item.model} className="border px-3 py-2" style={{ borderColor: C.border }}>{item.model}: {count(item.totalTokens)} tokens · {price(item.listPriceUsd)} ({coverage(item.tokenKnownTurns, item.turns)} with totals)</span>)}</div>
        {data.dayModelsTruncated && <div className="text-xs mt-2" style={{ color: C.amber }}>Additional models are omitted from this display.</div>}
      </div>}
      {data.daySources?.length > 0 && <div className="mt-5">
        <div className="font-semibold text-sm">Agent sources on this day</div>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">{data.daySources.map((item) => <span key={item.sourceKind} className="border px-3 py-2" style={{ borderColor: C.border }}>{item.sourceKind}: {count(item.totalTokens)} tokens · {price(item.listPriceUsd)} ({coverage(item.tokenKnownTurns, item.turns)} with totals)</span>)}</div>
        {data.daySourcesTruncated && <div className="text-xs mt-2" style={{ color: C.amber }}>Additional sources are omitted from this display.</div>}
      </div>}
      <div className="mt-6 font-semibold">Sessions active on {data.selectedDay} UTC</div>
      <p className="text-xs mt-1" style={{ color: C.sub }}>Session totals here cover turns that started on this day. Selecting one shows all its turns in the 30-day window.</p>
      {data.sessions.length ? <div className="mt-3 grid gap-2">{data.sessions.map((item) => <button key={`${item.sourceKind}:${item.sessionId}`} type="button" onClick={() => inspectSession(item)}
        className="w-full border p-3 text-left flex flex-wrap justify-between gap-2 text-sm"
        style={{ borderColor: item.sessionId === session?.sessionId && item.sourceKind === session?.sourceKind ? C.green : C.border }}>
        <span><strong>{item.agentName}</strong>{item.agentName === item.sourceKind ? "" : ` · ${item.sourceKind}`} · {item.runIds.length} run{item.runIds.length === 1 ? "" : "s"}</span>
        <span>{count(item.totalTokens)} tokens · {price(item.listPriceUsd)} · {coverage(item.tokenKnownTurns, item.turns)} with totals</span>
      </button>)}</div> : <div className="text-sm mt-3" style={{ color: C.sub }}>No captured turns started on this day.</div>}
      {data.sessionsTruncated && <div className="text-xs mt-2" style={{ color: C.amber }}>Showing the 100 sessions with the most reported tokens on this day.</div>}
      {data.selectionReason === "ambiguous_source" && <p role="status" className="text-sm mt-3" style={{ color: C.amber }}>This session ID appears in more than one agent source. Select the source-specific session above.</p>}
      {session && <div ref={selectedSessionRef} tabIndex={-1} className="mt-6 border-t pt-5" style={{ borderColor: C.border, scrollMarginTop: 24 }}>
        <div className="flex flex-wrap justify-between gap-3"><div><div className="font-semibold">Selected session</div>
          <div className="text-xs mt-1" style={{ color: C.sub }}>Full 30-day window: {count(session.totalTokens)} reported tokens across {session.turnCount} turns · {price(session.listPriceUsd)} estimated list price ({coverage(session.pricedTurns, session.turnCount)} priced)</div></div>
          <button type="button" onClick={() => setAnalyzing((value) => !value)} className="border px-3 py-2 text-sm" style={{ borderColor: C.green, color: C.green }}>{analyzing ? "Hide analysis" : "Analyze session"}</button></div>
        {analyzing && <SessionExplanation session={session} selectedDay={data.selectedDay} dayTotal={chosenDay?.totalTokens} />}
        <div className="font-semibold text-sm mt-5">Largest turns</div>
        <div className="mt-2 space-y-2">{session.turns.slice(0, compact ? 5 : undefined).map((turn) => <div key={`${turn.runId}:${turn.turnId}`} className="border p-3 flex flex-wrap justify-between gap-2 text-xs" style={{ borderColor: C.border }}>
          <span>Run {turn.runId} · Turn {turn.sequence + 1} · {turn.models.join(", ") || "model unavailable"} · {count(turn.totalTokens)} tokens · {price(turn.listPriceUsd)}</span>
          <button type="button" className="underline" style={{ color: C.green }} onClick={() => onOpenRun?.(turn.runId)}>Open run</button>
        </div>)}</div>
        {compact && session.turns.length > 5 && <details className="mt-3 text-sm"><summary className="cursor-pointer">Show {session.turns.length - 5} more displayed turns</summary>
          <div className="mt-2 space-y-2">{session.turns.slice(5).map((turn) => <div key={`${turn.runId}:${turn.turnId}`} className="border p-3 flex flex-wrap justify-between gap-2 text-xs" style={{ borderColor: C.border }}>
            <span>Run {turn.runId} · Turn {turn.sequence + 1} · {turn.models.join(", ") || "model unavailable"} · {count(turn.totalTokens)} tokens · {price(turn.listPriceUsd)}</span>
            <button type="button" className="underline" style={{ color: C.green }} onClick={() => onOpenRun?.(turn.runId)}>Open run</button>
          </div>)}</div></details>}
        {session.turnsTruncated && <div className="text-xs mt-2" style={{ color: C.amber }}>Showing the 50 largest turns; session totals include the remaining turns in this window.</div>}
        {compact && <button type="button" onClick={() => setReportOpen((value) => !value)} className="border px-3 py-2 text-sm mt-4" style={{ borderColor: C.border, color: C.green }}>{reportOpen ? "Hide session report" : "Open session report"}</button>}
        {(!compact || reportOpen) && <SessionReport key={`${session.sourceKind}:${session.sessionId}`} root={url.replace(/\/api\/usage$/, "")} sessionId={session.sessionId} sourceKind={session.sourceKind} onOpenRun={onOpenRun} />}
      </div>}
      <p className="text-xs mt-5" style={{ color: C.faint }}>Estimated list price uses the captured turn's tokens and one named model where supported. Missing or mixed-model turns remain unpriced. This is not a bill. Observed session span includes pauses and tool work; model-call latency is shown separately where captured in LLM Traces.</p>
      </>}
    </>}
  </section>;
}

function Fact({ label, value, detail }) {
  return <div className="border p-3" style={{ borderColor: C.border }}><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="font-semibold mt-1">{value}</div><div className="text-xs mt-1" style={{ color: C.sub }}>{detail}</div></div>;
}
