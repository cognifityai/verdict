import React, { useEffect, useRef, useState } from "react";

const box = { background: "#111715", borderColor: "#26332e", color: "#e5eee9" };
const muted = { color: "#94a39d" };

function formatScore(value) {
  return value == null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 3 });
}

function currentGrade(detail, fingerprint, dimension) {
  const grade = detail.assessments.find((item) => item.evaluator_fingerprint === fingerprint
    && item.target_position === null && item.revision === detail.conversation.revision);
  const definition = grade?.rubric?.dimensions?.find((item) => item.name === dimension);
  const outcome = grade?.dimensions?.[dimension];
  return { grade, definition, outcome };
}

export function MatchedConversationCompare({ configUrl, source }) {
  const root = configUrl.replace(/\/api\/config$/, "");
  const [token, setToken] = useState(null);
  const [form, setForm] = useState({ windowStart: "", windowEnd: "", evaluatorFingerprint: "",
    dimension: "", pairKey: "pair_id", variantKey: "variant", leftVariant: "", rightVariant: "" });
  const [result, setResult] = useState(null);
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const generation = useRef(0);
  useEffect(() => {
    if (source !== "live") return;
    fetch(`${root}/api/setup/token`, { credentials: "same-origin" })
      .then((response) => response.json()).then((body) => setToken(body.setupToken))
      .catch(() => setError("Could not load comparison access."));
  }, [root, source]);
  if (source !== "live") return null;

  const update = (key, value) => {
    generation.current += 1;
    setForm((current) => ({ ...current, [key]: value }));
    setResult(null); setDetail(null); setError(null); setBusy(false);
  };
  async function compare(event) {
    event.preventDefault();
    if (busy || !token) return;
    const requestId = ++generation.current;
    setBusy(true); setError(null); setResult(null); setDetail(null);
    try {
      const payload = { ...form, analysisUnit: "matched_conversation",
        windowStart: `${form.windowStart}T00:00:00Z`, windowEnd: `${form.windowEnd}T00:00:00Z` };
      const response = await fetch(`${root}/api/compare/conversations/matched`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Verdict-Setup": token },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      if (generation.current === requestId) setResult(body);
    } catch (failure) { if (generation.current === requestId) setError(String(failure)); }
    finally { if (generation.current === requestId) setBusy(false); }
  }
  async function openPair(pair) {
    if (busy || !result) return;
    const requestId = ++generation.current;
    setBusy(true); setError(null); setDetail(null);
    try {
      const load = async (side) => {
        const response = await fetch(`${root}/api/data/conversations/${encodeURIComponent(side.id)}?evaluator=${encodeURIComponent(result.evaluatorFingerprint)}`, { credentials: "same-origin" });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        return body;
      };
      const [left, right] = await Promise.all([load(pair.left), load(pair.right)]);
      const unchanged = [[pair.left, left, result.leftVariant], [pair.right, right, result.rightVariant]].every(([expected, body, variant]) => {
        const { grade, definition, outcome } = currentGrade(body, result.evaluatorFingerprint, result.dimension);
        return body.conversation.revision === expected.revision
          && body.conversation.labels?.[result.pairKey] === pair.pairId
          && body.conversation.labels?.[result.variantKey] === variant
          && grade?.status === expected.status && grade?.rubric?.target === "conversation"
          && definition?.type === expected.type && (definition?.direction ?? null) === expected.direction
          && (definition?.min ?? null) === expected.min && (definition?.max ?? null) === expected.max
          && outcome?.state === expected.state && outcome?.score === expected.score;
      });
      if (!unchanged) throw new Error("A conversation or grade changed. Run the comparison again.");
      if (generation.current === requestId) setDetail({ pairId: pair.pairId, left, right });
    } catch (failure) { if (generation.current === requestId) setError(String(failure)); }
    finally { if (generation.current === requestId) setBusy(false); }
  }
  return <section className="border p-5 space-y-4" style={box}>
    <div><h2 className="text-lg font-semibold">Compare declared conversation pairs</h2>
      <p className="text-sm mt-1" style={muted}>Choose a campaign window and two source variants. A shared pair ID is supplied by your data source; Verdict cannot prove the conversations received identical inputs. Both conversations must end inside the UTC window. This is an exploratory result, not an alert or a model winner.</p></div>
    <form onSubmit={compare} className="grid sm:grid-cols-2 gap-3 text-sm">
      {[["windowStart", "Start date (UTC, inclusive)", "date"], ["windowEnd", "End date (UTC, exclusive)", "date"],
        ["evaluatorFingerprint", "Evaluator fingerprint", "text"], ["dimension", "Rubric dimension", "text"],
        ["pairKey", "Pair ID label key", "text"], ["variantKey", "Variant label key", "text"],
        ["leftVariant", "First variant value", "text"], ["rightVariant", "Second variant value", "text"]]
        .map(([key, label, type]) => <label key={key} className="block">{label}
          <input aria-label={label} required type={type} value={form[key]} onChange={(event) => update(key, event.target.value)}
            className="block w-full mt-1 px-3 py-2 border rounded" style={{ ...box, minWidth: 0 }} /></label>)}
      <button disabled={busy || !token} className="px-4 py-2 border rounded font-semibold sm:col-span-2" style={{ borderColor: "#4ee1aa", color: "#4ee1aa" }}>Compare pairs</button>
    </form>
    {error && <p role="alert" className="text-sm" style={{ color: "#ff6b6b" }}>{error}</p>}
    {result && <div className="space-y-3 text-sm">
      <div className="grid sm:grid-cols-3 gap-3">
        <div>Selected variant rows <strong>{result.candidateRows}</strong></div>
        <div>One of each variant <strong>{result.declaredPairs}</strong></div>
        <div>Usable graded pairs <strong>{result.usablePairs}</strong></div>
      </div>
      <p style={muted}>Exclusions: {Object.entries(result.exclusions).filter(([, count]) => count > 0)
        .map(([name, count]) => `${name.replace(/([A-Z])/g, " $1").toLowerCase()}: ${count}`).join(" · ") || "none"}.
        {result.technicalFailurePairs > 0 && ` ${result.technicalFailurePairs} usable pairs include a technical-failure closure.`}</p>
      {result.binary && <p>Both pass: {result.binary.bothPass} · Both fail: {result.binary.bothFail} · First passes, second fails: {result.binary.leftPassRightFail} · First fails, second passes: {result.binary.leftFailRightPass}</p>}
      {result.numeric && <p>Mean score on paired cases: first {formatScore(result.numeric.leftMean)}, second {formatScore(result.numeric.rightMean)}; second minus first {formatScore(result.numeric.meanRightMinusLeft)}. Conservative 95% interval for the paired difference: {formatScore(result.numeric.deltaInterval95[0])} to {formatScore(result.numeric.deltaInterval95[1])} (assumes independent, representative cases). {result.direction === "lower_is_better" ? "Lower scores are better." : "Higher scores are better."}</p>}
      {!result.dimensionType && <p style={muted}>No current grade identifies this dimension yet. Grade both variants with the same whole-conversation rubric.</p>}
      {result.examples.length > 0 && <div><h3 className="font-semibold mb-2">Paired examples (up to 50)</h3>
        <div className="max-h-64 overflow-auto space-y-1">{result.examples.map((pair) => <button key={pair.pairId}
          disabled={busy} onClick={() => openPair(pair)} className="block w-full text-left px-3 py-2 border rounded"
          style={box}>{pair.pairId}: {pair.left.state}{pair.left.score != null ? ` ${formatScore(pair.left.score)}` : ""} → {pair.right.state}{pair.right.score != null ? ` ${formatScore(pair.right.score)}` : ""}</button>)}</div></div>}
    </div>}
    {detail && <div className="border-t pt-4 space-y-3" style={{ borderColor: "#26332e" }}>
      <div className="flex justify-between"><h3 className="font-semibold">Pair {detail.pairId}</h3><button onClick={() => setDetail(null)}>Close</button></div>
      <div className="grid md:grid-cols-2 gap-3">{[[result.leftVariant, detail.left], [result.rightVariant, detail.right]].map(([variant, side]) => <div key={variant} className="border p-3 space-y-2" style={box}>
        <h4 className="font-semibold">{variant}</h4><div className="text-xs" style={muted}>Conversation {side.conversation.id} · revision {side.conversation.revision.slice(0, 12)}</div>
        <div>Grade: {currentGrade(side, result.evaluatorFingerprint, result.dimension).outcome?.state}{result.dimensionType === "number" ? ` · score ${formatScore(currentGrade(side, result.evaluatorFingerprint, result.dimension).outcome?.score)}` : ""}</div>
        {side.conversation.messages.map((message, index) => <div key={index} className="border p-2" style={box}><strong>{message.role}</strong><p dir="auto" className="whitespace-pre-wrap">{message.content}</p></div>)}
      </div>)}</div>
    </div>}
  </section>;
}
