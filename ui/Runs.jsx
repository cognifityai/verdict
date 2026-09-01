import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, RefreshCw } from "lucide-react";

const color = {
  panel: "#111715", border: "#26332e", text: "#e7f0ec", sub: "#94a39d",
  faint: "#68766f", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b",
};

export function Runs({
  url, focusRunIds = [], selectedRunId: routedRunId = null,
  findingCode = null, runIdsTruncated = false, onSelectRun = null, onShowAll = null,
  evaluatorFingerprint = null,
}) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState({ loading: false, error: null, data: null });
  const [eventOffset, setEventOffset] = useState(0);
  const [turnOffset, setTurnOffset] = useState(0);
  const [focusEventId, setFocusEventId] = useState(null);
  const load = React.useCallback(() => {
    setState((current) => ({ ...current, loading: true, error: null }));
    const query = new URLSearchParams();
    focusRunIds.forEach((runId) => query.append("run_ids", runId));
    if (evaluatorFingerprint) query.set("evaluator_fingerprint", evaluatorFingerprint);
    const listUrl = query.size
      ? `${url}${url.includes("?") ? "&" : "?"}${query.toString()}`
      : url;
    fetch(listUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((error) => setState({ loading: false, error: String(error), data: null }));
  }, [evaluatorFingerprint, focusRunIds.join("\u0000"), url]);
  useEffect(load, [load]);

  useEffect(() => {
    setSelected(routedRunId);
    setEventOffset(0);
    setTurnOffset(0);
  }, [routedRunId]);

  const runs = state.data?.runs || [];
  const selectedRunId = routedRunId || selected || runs[0]?.runId || null;
  useEffect(() => {
    if (!selectedRunId) return;
    const controller = new AbortController();
    setDetail({ loading: true, error: null, data: null });
    const eventFocus = focusEventId ? `&event_id=${encodeURIComponent(focusEventId)}` : "";
    fetch(`${url}/${encodeURIComponent(selectedRunId)}?event_limit=100&event_offset=${eventOffset}&turn_limit=20&turn_offset=${turnOffset}${eventFocus}`, {
      credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((data) => setDetail({ loading: false, error: null, data }))
      .catch((error) => {
        if (error?.name !== "AbortError") setDetail({ loading: false, error: String(error), data: null });
      });
    return () => controller.abort();
  }, [eventOffset, focusEventId, selectedRunId, turnOffset, url]);

  if (state.error) return <Notice icon={AlertTriangle} text={`Runs unavailable: ${state.error}`} />;
  if (!state.data) return <Notice icon={RefreshCw} text="Loading agent runs…" />;
  if (!runs.length) {
    return <Notice icon={CheckCircle2} text="No agent runs captured yet. Run verdict-import local, then refresh." />;
  }
  return (
    <div className="grid lg:grid-cols-[360px_minmax(0,1fr)] gap-4">
      <section className="border" style={{ borderColor: color.border, background: color.panel }}>
        {focusRunIds.length > 0 && <div className="p-3 border-b text-xs flex items-center justify-between gap-3" style={{ borderColor: color.amber, color: color.amber }}>
          <span>Finding {findingCode || "evidence"}: showing {runs.length} of {focusRunIds.length}{runIdsTruncated ? "+" : ""} affected runs{state.data?.filter?.complete === false ? " (some are unavailable in this tenant)" : ""}.</span>
          <button className="underline shrink-0" onClick={onShowAll}>Show all runs</button>
        </div>}
        <div className="p-4 border-b flex items-center justify-between" style={{ borderColor: color.border }}>
          <div>
            <div className="font-semibold">Agent runs</div>
            <div className="text-xs mt-1" style={{ color: color.sub }}>
              {state.data.summary.shown} of {state.data.summary.available} sessions
            </div>
          </div>
          <button onClick={load} title="Refresh runs"><RefreshCw size={15} /></button>
        </div>
        {runs.map((run) => {
          const active = (selected || runs[0].runId) === run.runId;
          const errors = run.findings.filter((finding) => finding.severity === "error").length;
          return (
            <button key={run.runId} onClick={() => { setSelected(run.runId); onSelectRun?.(run.runId); setFocusEventId(null); setEventOffset(0); setTurnOffset(0); }}
              className="w-full text-left p-4 border-b flex items-start gap-3"
              style={{ borderColor: color.border, background: active ? "#18221e" : "transparent" }}>
              <ChevronRight size={15} style={{ marginTop: 2, color: active ? color.green : color.faint }} />
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold">{run.sourceKind}</span>
                <span className="block text-xs mt-1" style={{ color: color.sub }}>
                  {run.turnCount} turns · {run.eventCount} events · {errors} observed errors
                </span>
                <span className="block text-xs mt-1 font-mono truncate" style={{ color: color.faint }}>{run.startedAt}</span>
              </span>
            </button>
          );
        })}
      </section>
      <RunDetail run={runs.find((run) => run.runId === selectedRunId)} detail={detail} onEventPage={(offset) => { setFocusEventId(null); setEventOffset(offset); }} onTurnPage={setTurnOffset} onFocusEvent={(eventId) => { setFocusEventId(eventId); setEventOffset(0); }} focusEventId={focusEventId} />
    </div>
  );
}

function RunDetail({ run, detail, onEventPage, onTurnPage, onFocusEvent, focusEventId }) {
  if (!run) return null;
  const metrics = run.metrics || {};
  return (
    <section className="border p-5 min-w-0" style={{ borderColor: color.border, background: color.panel }}>
      <div className="flex flex-wrap justify-between gap-3">
        <div>
          <div className="text-xs font-mono" style={{ color: color.green }}>{run.sourceKind.toUpperCase()}</div>
          <div className="font-semibold mt-1">{run.status === "unknown" ? "Session status unavailable" : run.status}</div>
        </div>
        <div className="text-xs" style={{ color: color.sub }}>
          {metrics.model_calls || 0} model calls · {metrics.tool_calls || 0} tool calls · {metrics.input_tokens || 0} input tokens
        </div>
      </div>
      <div className="mt-5 grid sm:grid-cols-3 gap-2">
        {Object.entries(run.evidenceCoverage || {}).map(([name, value]) => (
          <div key={name} className="border p-3" style={{ borderColor: color.border }}>
            <div className="text-xs" style={{ color: color.faint }}>{name.replaceAll("_", " ")}</div>
            <div className="text-sm mt-1">{value.replaceAll("_", " ")}</div>
          </div>
        ))}
      </div>
      <div className="mt-4 grid sm:grid-cols-4 gap-2">
        <StatusFact label="Source outcome" value={run.sourceOutcome || run.status} />
        <StatusFact label="Turn outcomes" value={displayCounts(run.turnOutcomes)} />
        <StatusFact label="Finding severity" value={displayCounts(run.findingSeverity)} />
        <StatusFact label="Selected evaluator" value={run.evaluationCoverage?.state === "selected" ? `${run.evaluationCoverage.judged} judged · ${run.evaluationCoverage.notJudged} not judged · ${run.evaluationCoverage.judgeErrors} errors` : "Not selected"} />
      </div>
      <h2 className="font-semibold mt-6">Deterministic findings</h2>
      <div className="mt-2 space-y-2">
        {run.findings.length ? run.findings.map((finding, index) => (
          <button key={`${finding.code}-${index}`} disabled={!finding.evidenceEventIds?.length} onClick={() => onFocusEvent(finding.evidenceEventIds[0])} className="border p-3 text-sm text-left w-full" style={{ borderColor: color.border }}>
            <span className="font-mono text-xs" style={{ color: finding.severity === "error" ? color.red : finding.severity === "warning" ? color.amber : color.sub }}>{finding.code}</span>
            <div className="mt-1">{finding.message}</div>
            <div className="text-xs mt-1" style={{ color: color.faint }}>{finding.evidenceEventIds?.length ? `Open ${finding.evidenceEventIds.length} linked evidence event(s)` : "No event-level evidence is available for this finding."}</div>
          </button>
        )) : <div className="text-sm" style={{ color: color.sub }}>No deterministic findings.</div>}
      </div>
      <h2 className="font-semibold mt-6">Turns</h2>
      <div className="mt-2 space-y-2">
        {(detail.data?.turns || []).map((turn) => (
          <details key={turn.turnId} className="border p-3" style={{ borderColor: color.border }}>
            <summary className="text-sm cursor-pointer">Turn {turn.sequence + 1} · {turn.status}</summary>
            <div className="mt-3 text-xs" style={{ color: color.sub }}>
              <div>Request ({turn.requestState}): {turn.request ?? "not available"}</div>
              <div className="mt-2">Response ({turn.responseState}): {turn.response ?? "not available"}</div>
            </div>
          </details>
        ))}
        {detail.data?.turnPage && <div className="flex items-center justify-between gap-2 text-xs" style={{ color: color.sub }}><span>Showing {detail.data.turnPage.offset + 1}-{detail.data.turnPage.offset + detail.data.turnPage.shown} of {detail.data.turnPage.available} turns.</span><span className="flex gap-2"><button disabled={detail.data.turnPage.offset === 0} onClick={() => onTurnPage(Math.max(0, detail.data.turnPage.offset - detail.data.turnPage.limit))} className="border px-3 py-1">Previous</button><button disabled={!detail.data.turnPage.truncated} onClick={() => onTurnPage(detail.data.turnPage.offset + detail.data.turnPage.limit)} className="border px-3 py-1">Next</button></span></div>}
      </div>
      <h2 className="font-semibold mt-6">Execution timeline</h2>
      {detail.loading && <div className="text-sm mt-2" style={{ color: color.sub }}>Loading ordered evidence…</div>}
      {detail.error && <div role="alert" className="text-sm mt-2" style={{ color: color.red }}>{detail.error}</div>}
      {detail.data && <div className="mt-2 space-y-2">
        {detail.data.events.map((event) => <EventRow key={event.eventId} event={event} focused={event.eventId === (detail.data.focusEventId || focusEventId)} />)}
        {!detail.data.events.length && <div className="text-sm" style={{ color: color.sub }}>No normalized events were captured for this run.</div>}
        <div className="flex items-center justify-between gap-2 text-xs" style={{ color: color.sub }}><span>Showing {detail.data.page.offset + 1}-{detail.data.page.offset + detail.data.page.shown} of {detail.data.page.available} events.</span><span className="flex gap-2"><button disabled={detail.data.page.offset === 0} onClick={() => onEventPage(Math.max(0, detail.data.page.offset - detail.data.page.limit))} className="border px-3 py-1">Previous</button><button disabled={!detail.data.page.truncated} onClick={() => onEventPage(detail.data.page.offset + detail.data.page.limit)} className="border px-3 py-1">Next</button></span></div>
      </div>}
    </section>
  );
}

function displayCounts(values) {
  return Object.entries(values || {}).map(([name, count]) => `${name}: ${count}`).join(" · ") || "None";
}

function StatusFact({ label, value }) {
  return <div className="border p-3" style={{ borderColor: color.border }}><div className="text-xs" style={{ color: color.faint }}>{label}</div><div className="text-sm mt-1">{value}</div></div>;
}

function EventRow({ event, focused = false }) {
  const attributes = Object.entries(event.attributes || {});
  const failed = event.status === "failed";
  return (
    <details id={`event-${event.eventId}`} open={focused} className="border p-3" style={{ borderColor: failed ? color.red : focused ? color.amber : color.border }}>
      <summary className="text-sm cursor-pointer flex flex-wrap gap-2">
        <span className="font-mono" style={{ color: failed ? color.red : color.green }}>#{event.timelineIndex + 1}</span>
        <span>{event.type.replaceAll("_", " ")}</span>
        <span style={{ color: color.sub }}>{event.status}</span>
        {event.traceId && <span className="font-mono" style={{ color: color.amber }}>Trace {event.traceId}</span>}
        {event.judgment?.dimensions?.map((dimension) => <span key={dimension.name} className="font-mono" style={{ color: dimension.verdict === "pass" ? color.green : dimension.verdict === "fail" ? color.red : color.amber }}>{dimension.name}: {dimension.verdict}</span>)}
        <span className="ml-auto text-xs font-mono" style={{ color: color.faint }}>{event.occurredAt}</span>
      </summary>
      <div className="mt-3 grid gap-2 text-xs">
        <div style={{ color: color.faint }}>Source: {event.provenance} · privacy: {event.privacy}</div>
        {event.omissionReason && <div style={{ color: color.amber }}>Omitted: {event.omissionReason}</div>}
        {attributes.map(([name, value]) => <div key={name} className="grid sm:grid-cols-[140px_minmax(0,1fr)] gap-2">
          <span className="font-mono" style={{ color: color.sub }}>{name}</span>
          <pre className="whitespace-pre-wrap break-words" style={{ color: color.text }}>{typeof value === "string" ? value : JSON.stringify(value)}</pre>
        </div>)}
      </div>
    </details>
  );
}

function Notice({ icon: Icon, text }) {
  return <div className="border p-5 flex items-center gap-3 text-sm" style={{ borderColor: color.border, background: color.panel, color: color.sub }}><Icon size={16} />{text}</div>;
}
