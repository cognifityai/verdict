import React, { useEffect, useRef, useState } from "react";

const border = "#26332e";
const muted = "#94a39d";
const panel = { borderColor: border, background: "#111715" };

async function readJson(url) {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) throw new Error("Results could not be loaded.");
  return response.json();
}

export function ConversationResults({ root, source, traceResults }) {
  const [mode, setMode] = useState(null);
  const [identities, setIdentities] = useState([]);
  const [identityCursor, setIdentityCursor] = useState(null);
  const [nextIdentityCursor, setNextIdentityCursor] = useState(null);
  const [identityBusy, setIdentityBusy] = useState(source === "live");
  const [identityError, setIdentityError] = useState(false);
  const [selected, setSelected] = useState(null);
  const [pageCursor, setPageCursor] = useState(null);
  const [page, setPage] = useState(null);
  const [pageBusy, setPageBusy] = useState(false);
  const [pageError, setPageError] = useState(false);
  const [detailId, setDetailId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const [reload, setReload] = useState(0);
  const identityGeneration = useRef(0);
  const pageGeneration = useRef(0);
  const detailGeneration = useRef(0);

  useEffect(() => {
    if (source !== "live") return undefined;
    const generation = ++identityGeneration.current;
    setIdentityBusy(true); setIdentityError(false);
    const query = identityCursor ? `?after=${encodeURIComponent(identityCursor)}` : "";
    readJson(`${root}/api/data/conversations/assessment-evaluators${query}`)
      .then((value) => {
        if (identityGeneration.current !== generation) return;
        setIdentities((current) => identityCursor ? [...current, ...value.evaluators] : value.evaluators);
        setNextIdentityCursor(value.nextCursor);
        if (!identityCursor && value.evaluators.length) {
          setSelected((current) => current || value.evaluators[0].fingerprint);
        }
      })
      .catch(() => { if (identityGeneration.current === generation) setIdentityError(true); })
      .finally(() => { if (identityGeneration.current === generation) setIdentityBusy(false); });
    return () => { identityGeneration.current += 1; };
  }, [root, source, identityCursor, reload]);

  useEffect(() => {
    if (source !== "live" || !selected) return undefined;
    const generation = ++pageGeneration.current;
    setPageBusy(true); setPageError(false);
    const query = new URLSearchParams({ evaluator: selected });
    if (pageCursor) query.set("after", pageCursor);
    readJson(`${root}/api/data/conversations/assessments?${query}`)
      .then((value) => { if (pageGeneration.current === generation) setPage(value); })
      .catch(() => { if (pageGeneration.current === generation) setPageError(true); })
      .finally(() => { if (pageGeneration.current === generation) setPageBusy(false); });
    return () => { pageGeneration.current += 1; };
  }, [root, source, selected, pageCursor, reload]);

  useEffect(() => {
    if (source !== "live" || !selected || !detailId) return undefined;
    const generation = ++detailGeneration.current;
    const expected = page?.conversations.find((item) => item.id === detailId);
    setDetailBusy(true); setDetailError(false);
    readJson(`${root}/api/data/conversations/${encodeURIComponent(detailId)}?evaluator=${encodeURIComponent(selected)}`)
      .then((value) => {
        if (detailGeneration.current !== generation) return;
        if (!expected || value.conversation.revision !== expected.revision
          || !value.assessments.length || value.assessments.some((item) =>
            item.evaluator_fingerprint !== selected || item.revision !== expected.revision)) {
          setDetailError(true);
          return;
        }
        setDetail(value);
      })
      .catch(() => { if (detailGeneration.current === generation) setDetailError(true); })
      .finally(() => { if (detailGeneration.current === generation) setDetailBusy(false); });
    return () => { detailGeneration.current += 1; };
  }, [root, source, selected, detailId, page, reload]);

  if (source !== "live") return traceResults;
  const activeMode = mode || (identityBusy || identityError || identities.length ? "conversation" : "trace");
  const changeEvaluator = (fingerprint) => {
    pageGeneration.current += 1; detailGeneration.current += 1;
    setSelected(fingerprint); setPageCursor(null); setPage(null); setDetailId(null);
    setDetail(null); setPageError(false); setDetailError(false);
  };
  const refresh = () => {
    identityGeneration.current += 1; pageGeneration.current += 1; detailGeneration.current += 1;
    setIdentityCursor(null); setNextIdentityCursor(null); setIdentities([]);
    setSelected(null); setPageCursor(null); setPage(null); setDetailId(null); setDetail(null);
    setIdentityError(false); setPageError(false); setDetailError(false);
    setReload((current) => current + 1);
  };

  return <div className="space-y-5">
    <div className="flex flex-wrap items-center gap-2">
      <button onClick={() => setMode("conversation")} aria-pressed={activeMode === "conversation"}
        className="border px-3 py-2 text-sm" style={{ borderColor: activeMode === "conversation" ? "#4ee1aa" : border }}>Conversation results</button>
      <button onClick={() => setMode("trace")} aria-pressed={activeMode === "trace"}
        className="border px-3 py-2 text-sm" style={{ borderColor: activeMode === "trace" ? "#4ee1aa" : border }}>Trace results</button>
      <button onClick={refresh} className="ml-auto border px-3 py-2 text-sm" style={{ borderColor: border }}>Refresh results</button>
    </div>
    {activeMode === "trace" ? traceResults : <section className="border p-5 space-y-4" style={panel}>
      <div><h2 className="text-lg font-semibold">Stored conversation grades</h2>
        <p className="text-sm mt-1" style={{ color: muted }}>Read existing grades by evaluator. This screen does not run a judge or calculate drift. Reply grading may be partial.</p></div>
      {identityBusy && !identities.length && <p role="status">Loading evaluators…</p>}
      {identityError && <p role="alert">Could not load conversation evaluators.</p>}
      {!identityBusy && !identityError && !identities.length && <p>No conversation grades are stored yet.</p>}
      {!!identities.length && <>
        <label className="block text-sm">Evaluator
          <select aria-label="Conversation evaluator" className="block w-full border p-2 mt-1 bg-transparent" style={{ borderColor: border }}
            value={selected || ""} onChange={(event) => changeEvaluator(event.target.value)}>
            {identities.map((item) => <option key={item.fingerprint} value={item.fingerprint}>
              {item.rubricName} v{item.rubricVersion} · {item.provider}/{item.model} · {item.target} · {item.fingerprint.slice(0, 8)}
            </option>)}
          </select>
        </label>
        {selected && <p className="text-xs break-all" style={{ color: muted }}>Evaluator ID for Monitor: <code>{selected}</code></p>}
        {nextIdentityCursor && <button disabled={identityBusy} onClick={() => setIdentityCursor(nextIdentityCursor)} className="border px-3 py-2 text-sm">Load more evaluators</button>}
        {pageBusy && <p role="status">Loading grades…</p>}
        {pageError && <p role="alert">Could not load grades for this evaluator.</p>}
        {!pageBusy && !pageError && page && !page.conversations.length && <p>No current grades for this evaluator.</p>}
        {!pageBusy && !pageError && page?.conversations.length > 0 && <>
          <div className="overflow-auto max-h-96"><table className="w-full text-sm text-left">
            <thead><tr><th className="p-2">Conversation</th><th className="p-2">Stored grades</th></tr></thead>
            <tbody>{page.conversations.map((row) => <tr key={row.id} className="border-t" style={{ borderColor: border }}>
              <td className="p-2"><button className="underline" onClick={() => { detailGeneration.current += 1; setDetail(null); setDetailId(row.id); }}>{row.displayLabel || "Unnamed conversation"}</button>
                <div className="text-xs" style={{ color: muted }}>ID {row.id.slice(0, 12)}</div>
                <div className="text-xs" style={{ color: muted }}>{row.event_at
                  ? <>{row.event_at.slice(0, 10)}<br />{row.event_at.slice(11, 16)} UTC</>
                  : "No source time"}</div></td>
              <td className="p-2">{row.completedCount} completed · {row.errorCount} errors</td>
            </tr>)}</tbody>
          </table></div>
          {page.nextCursor && <button disabled={pageBusy} onClick={() => { pageGeneration.current += 1; detailGeneration.current += 1; setPage(null); setDetailId(null); setDetail(null); setPageCursor(page.nextCursor); }} className="border px-3 py-2 text-sm">Next page</button>}
        </>}
        {detailBusy && <p role="status">Loading conversation evidence…</p>}
        {detailError && <p role="alert">The grade changed or could not be loaded. Refresh results.</p>}
        {detail && <div className="border p-4 space-y-3" style={{ borderColor: border }}>
          <div className="flex justify-between gap-3"><h3 className="font-semibold">Conversation evidence</h3><button className="underline text-sm" onClick={() => { detailGeneration.current += 1; setDetailId(null); setDetail(null); }}>Close</button></div>
          {detail.assessments.map((assessment) => <div key={assessment.id} className="border p-3 text-sm" style={{ borderColor: border }}>
            <strong>{assessment.rubric.name} v{assessment.rubric.version}</strong> · {assessment.target_position == null ? "whole conversation" : `reply at message ${assessment.target_position + 1}`} · {assessment.status}
            {Object.entries(assessment.dimensions).map(([name, value]) => <p key={name}>{name}: {value.score == null ? value.state : value.state === "unclear" ? value.score : `${value.score} (${value.state})`} · {value.reason}</p>)}
            {assessment.findings.map((finding, index) => <p key={index}>{finding.issue}: {finding.reason}{finding.quote && ` · “${finding.quote}”`}</p>)}
          </div>)}
          <div className="max-h-96 overflow-auto space-y-2">{detail.conversation.messages.map((message, index) => <div key={index} className="border p-3 text-sm" style={{ borderColor: border }}>
            <strong>{index + 1} · {message.role} · {message.status}</strong><p dir="auto" className="whitespace-pre-wrap mt-1">{message.content}</p>
          </div>)}</div>
        </div>}
      </>}
    </section>}
  </div>;
}
