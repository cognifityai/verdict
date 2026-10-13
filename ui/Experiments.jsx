import React, { useEffect, useState } from "react";

const C = { border: "#26332e", sub: "#94a39d", green: "#4ee1aa", amber: "#f2b84b", red: "#ff6b6b" };
const newCase = () => ({ id: "", input: "", context: "", expectedBehavior: "", kind: "exact", value: "" });

export function Experiments({ root, token, draft = null }) {
  const [localToken, setLocalToken] = useState(null);
  const [view, setView] = useState({ loading: true, data: null, error: null });
  const [caseSetId, setCaseSetId] = useState("example_cases");
  const [title, setTitle] = useState("Representative tasks");
  const [cases, setCases] = useState([newCase()]);
  const [reviewed, setReviewed] = useState(false);
  const [selected, setSelected] = useState("");
  const [baseline, setBaseline] = useState("");
  const [candidate, setCandidate] = useState("");
  const [trials, setTrials] = useState(1);
  const [busy, setBusy] = useState(false);
  const [selectedExperimentId, setSelectedExperimentId] = useState("");
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!draft) return;
    setCaseSetId(`captured_${Date.now()}`);
    setTitle("Reviewed captured task");
    setCases([{ ...newCase(), id: "captured_task", input: draft.complete ? draft.request : "" }]);
    setReviewed(false);
  }, [draft]);
  const refresh = async () => {
    setView((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(`${root}/api/experiments`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      setView({ loading: false, data, error: null });
      setSelected((current) => current || data.caseSets?.[0]?.documentId || "");
      setSelectedExperimentId((current) => data.experiments?.some((item) => item.documentId === current)
        ? current : data.experiments?.[0]?.documentId || "");
      setBaseline((current) => current || data.configuredVariants?.[0] || "");
      setCandidate((current) => current || data.configuredVariants?.[1] || "");
    } catch (failure) { setView({ loading: false, data: null, error: String(failure) }); }
  };
  useEffect(() => {
    refresh();
    if (!token) fetch(`${root}/api/setup/token`, { credentials: "same-origin" })
      .then((response) => response.json()).then((value) => setLocalToken(value.setupToken))
      .catch((failure) => setError(String(failure)));
  }, [root, token]);
  const authorization = token || localToken;
  const updateCase = (index, change) => {
    setCases((current) => current.map((item, i) => i === index ? { ...item, ...change } : item));
    setReviewed(false);
  };
  const post = async (path, payload) => {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${root}${path}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": authorization },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } catch (failure) { setError(String(failure)); return null; }
    finally { setBusy(false); }
  };
  const save = async () => {
    const payload = { title, reviewed: true, cases: cases.map((item) => ({
      id: item.id, input: item.input, expectedBehavior: item.expectedBehavior,
      ...(item.context ? { context: item.context } : {}),
      checks: [{ kind: item.kind, value: item.value }],
    })) };
    const saved = await post(`/api/experiments/case-sets/${encodeURIComponent(caseSetId)}`, payload);
    if (saved) { setSelected(caseSetId); setReviewed(false); await refresh(); }
  };
  const data = view.data;
  const selectedCaseSet = data?.caseSets?.find((item) => item.documentId === selected);
  const plannedRunnerSeconds = selectedCaseSet && Number.isFinite(data?.maxRunSeconds) && data?.trialTimeoutSeconds != null
    ? selectedCaseSet.payload.cases.length * 2 * trials * data.trialTimeoutSeconds : null;
  const overRunLimit = plannedRunnerSeconds != null && plannedRunnerSeconds > data.maxRunSeconds;
  const run = async () => {
    const next = await post("/api/experiments/run", {
      caseSetId: selected, baseline, candidate, trials,
    });
    if (next) { setSelectedExperimentId(next.experimentId); await refresh(); }
  };
  const selectedExperiment = data?.experiments?.find((item) => item.documentId === selectedExperimentId);
  const result = selectedExperiment?.payload;
  return <section className="border p-5 space-y-4" style={{ borderColor: C.border }}>
    <div><div className="text-xs font-mono" style={{ color: C.green }}>SAVED CASES</div>
      <h2 className="font-semibold mt-1">Test a change on the same tasks</h2>
      <p className="text-sm mt-2" style={{ color: C.sub }}>Review runnable inputs and expected behavior, then compare a fixed case set across two locally configured application versions. This invokes only the trusted runner configured on this host.</p></div>
    {view.loading && <p role="status" className="text-sm">Loading saved cases…</p>}
    {view.error && <p role="alert" className="text-sm" style={{ color: C.red }}>{view.error}</p>}
    {draft && <p className="border p-3 text-xs" style={{ borderColor: C.amber, color: C.amber }}>
      Draft from captured run {draft.runId}{draft.turnId ? `, turn ${draft.turnId}` : ""}. {draft.complete
        ? "The recorded, redacted request was copied into runnable input. Review and edit it for your configured task runner."
        : "No complete captured request was available. Enter runnable input manually."} Add expected behavior and a check before saving; capture alone cannot establish the right answer.
    </p>}
    <div className="grid sm:grid-cols-2 gap-3">
      <label className="text-xs">Case set ID<input className="block border p-2 mt-1 w-full bg-transparent" value={caseSetId} onChange={(event) => { setCaseSetId(event.target.value); setReviewed(false); }} /></label>
      <label className="text-xs">Title<input className="block border p-2 mt-1 w-full bg-transparent" value={title} onChange={(event) => { setTitle(event.target.value); setReviewed(false); }} /></label>
    </div>
    {cases.map((item, index) => <div key={index} className="border p-3 grid sm:grid-cols-2 gap-2 text-xs" style={{ borderColor: C.border }}>
      <label>Case ID<input className="block border p-2 mt-1 w-full bg-transparent" value={item.id} onChange={(event) => updateCase(index, { id: event.target.value })} /></label>
      <label>Expected behavior<input className="block border p-2 mt-1 w-full bg-transparent" value={item.expectedBehavior} onChange={(event) => updateCase(index, { expectedBehavior: event.target.value })} /></label>
      <label>Runnable input<textarea className="block border p-2 mt-1 w-full bg-transparent" value={item.input} onChange={(event) => updateCase(index, { input: event.target.value })} /></label>
      <label>Relevant context (optional)<textarea className="block border p-2 mt-1 w-full bg-transparent" value={item.context} onChange={(event) => updateCase(index, { context: event.target.value })} /></label>
      <label>Check<select className="block border p-2 mt-1 w-full bg-transparent" value={item.kind} onChange={(event) => updateCase(index, { kind: event.target.value })}><option value="exact">Exact output</option><option value="contains">Contains</option><option value="not_contains">Does not contain</option></select></label>
      <label>Check value<input className="block border p-2 mt-1 w-full bg-transparent" value={item.value} onChange={(event) => updateCase(index, { value: event.target.value })} /></label>
    </div>)}
    <div className="flex flex-wrap gap-2 text-xs"><button className="border px-3 py-2" disabled={cases.length >= 10 || busy} onClick={() => { setCases((items) => [...items, newCase()]); setReviewed(false); }}>Add case</button>{cases.length > 1 && <button className="border px-3 py-2" disabled={busy} onClick={() => { setCases((items) => items.slice(0, -1)); setReviewed(false); }}>Remove last</button>}</div>
    <label className="flex gap-2 text-xs"><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />I reviewed each runnable input, context, expected behavior, and check. Saved case sets are stored and passed to the configured local runner exactly as entered. Remove secrets or private data before saving. Captured history is never replayed automatically.</label>
    <button className="border px-4 py-2 text-sm" disabled={!authorization || !reviewed || busy} onClick={save}>Save reviewed case set</button>
    <div className="border-t pt-4 grid sm:grid-cols-4 gap-3 text-xs" style={{ borderColor: C.border }}>
      <label>Saved case set<select className="block border p-2 mt-1 w-full bg-transparent" value={selected} onChange={(event) => setSelected(event.target.value)}><option value="">Select…</option>{data?.caseSets?.filter((item) => item.state === "active").map((item) => <option key={item.documentId} value={item.documentId}>{item.documentId} · v{item.revision}</option>)}</select></label>
      <label>Baseline<select className="block border p-2 mt-1 w-full bg-transparent" value={baseline} onChange={(event) => setBaseline(event.target.value)}>{data?.configuredVariants?.map((name) => <option key={name}>{name}</option>)}</select></label>
      <label>Candidate<select className="block border p-2 mt-1 w-full bg-transparent" value={candidate} onChange={(event) => setCandidate(event.target.value)}>{data?.configuredVariants?.map((name) => <option key={name}>{name}</option>)}</select></label>
      <label>Trials per side<select className="block border p-2 mt-1 w-full bg-transparent" value={trials} onChange={(event) => setTrials(Number(event.target.value))}><option value={1}>1</option><option value={2}>2</option><option value={3}>3</option></select></label>
    </div>
    {!data?.runnerConfigured && <p className="text-xs" style={{ color: C.amber }}>Configure a trusted local runner and named application versions before running cases.</p>}
    {data?.trialTimeoutSeconds != null && <p className="text-xs" style={{ color: C.sub }}>Host trial timeout: {data.trialTimeoutSeconds} seconds. The runner and its ordinary child process group are stopped on timeout; this is not a security sandbox.</p>}
    {plannedRunnerSeconds != null && <p className="text-xs" style={{ color: overRunLimit ? C.amber : C.sub }}>Configured runner limit: {plannedRunnerSeconds} of {data.maxRunSeconds} seconds across both sides. {overRunLimit ? "Use fewer cases or trials, or lower the host trial timeout." : "Process cleanup and storage may add time."}</p>}
    <button className="border px-4 py-2 text-sm" disabled={!authorization || !selected || !baseline || !candidate || baseline === candidate || !data?.runnerConfigured || overRunLimit || busy} onClick={run}>{busy ? "Running…" : "Run baseline and candidate"}</button>
    {error && <p role="alert" className="text-xs" style={{ color: C.red }}>{error}</p>}
    {data?.experiments?.length > 0 && <label className="block text-xs">Saved comparison
      <select className="block border p-2 mt-1 w-full bg-transparent" value={selectedExperimentId}
        onChange={(event) => setSelectedExperimentId(event.target.value)}>
        {data.experiments.map((item) => <option key={item.documentId} value={item.documentId}>
          {item.payload?.caseSetId || item.documentId} · {item.payload?.baseline || "baseline"} → {item.payload?.candidate || "candidate"} · {item.createdAt}
        </option>)}
      </select>
    </label>}
    {result && <div className="border p-4 text-xs space-y-2" style={{ borderColor: C.border }}><h3 className="font-semibold">Fixed-case comparison · {result.caseSetId} v{result.caseSetRevision}</h3>
      <p>Saved {selectedExperiment.createdAt} · {selectedExperiment.state} · {result.baseline} ({result.baselineVersion}) → {result.candidate} ({result.candidateVersion}) · {result.trialsPerSide} trial(s) per side</p>
      <p>Evaluator: {result.evaluatorIdentity} · Configuration: <span className="break-all">{result.configurationFingerprint}</span></p>
      <p>Fixed: {result.comparison.fixed.join(", ") || "none"} · New regressions: {result.comparison.newRegressions.join(", ") || "none"} · Unchanged passes: {result.comparison.unchangedPasses.join(", ") || "none"} · Unverified: {result.comparison.unverified.join(", ") || "none"}</p>
      {result.cases.map((item) => <div key={item.id} className="border p-2" style={{ borderColor: C.border }}>{item.id}: {item.baseline.status} → {item.candidate.status} · {item.expectedBehavior}
        {item.comparableMetrics?.taskElapsedMs && <p>Runner elapsed: {item.comparableMetrics.taskElapsedMs[0].toFixed(1)} → {item.comparableMetrics.taskElapsedMs[1].toFixed(1)} ms average per trial (includes application work, not model latency)</p>}
        {item.comparableMetrics?.reportedCostUsd && <p>Application-reported cost ({item.comparableMetrics.reportedCostBasis}): ${item.comparableMetrics.reportedCostUsd[0].toFixed(4)} → ${item.comparableMetrics.reportedCostUsd[1].toFixed(4)} average per trial</p>}
        <details className="mt-2"><summary>Trial outputs and errors</summary>
          {[["Baseline", item.baseline], ["Candidate", item.candidate]].map(([label, side]) =>
            <div key={label} className="mt-2"><strong>{label}: {side.status}</strong>
              {side.trials.map((trial, index) => <div key={index} className="border p-2 mt-1" style={{ borderColor: C.border }}>
                Trial {index + 1}: {trial.status}{trial.error ? ` · ${trial.error}` : ""}
                {trial.output != null && <pre className="whitespace-pre-wrap break-all mt-1">{trial.output}</pre>}
                {trial.taskElapsedMs != null && <p>Runner elapsed: {trial.taskElapsedMs} ms</p>}
                {trial.costUsd != null && <p>Application-reported cost ({trial.costBasis}): ${trial.costUsd}</p>}
              </div>)}
            </div>)}
        </details>
      </div>)}
      <p style={{ color: C.sub }}>{result.claim} One trial does not establish behavior under nondeterminism.</p>
    </div>}
  </section>;
}
