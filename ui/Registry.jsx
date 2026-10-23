import React, { useEffect, useRef, useState } from "react";
import {
  Activity, AlertTriangle, BarChart3, CheckCircle2, Clock, GitBranch, Layers, RefreshCw,
} from "lucide-react";

import { useOperations } from "./Operations.jsx";
import { assignmentExplanation, normalizeRegistryPayload } from "./registry-data.js";
import { EvidenceExplain } from "./EvidenceExplain.jsx";

const COLOR = {
  bg: "#0b0e0d", panel: "#111615", panel2: "#151b19", border: "#2b3532",
  text: "#edf4f1", sub: "#9aaba4", faint: "#68766f", accent: "#4ee1aa",
  blue: "#56b6ff", green: "#56d39b", red: "#ff746a", amber: "#efbd63",
  redBg: "#2d1918", amberBg: "#2a2418", greenBg: "#172a22",
};

function Panel({ children, className = "", style = {} }) {
  return <section className={`border ${className}`} style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 3, ...style }}>{children}</section>;
}

function Pill({ children, color = COLOR.sub, background = COLOR.panel2 }) {
  return <span className="inline-flex items-center px-2 py-1 text-xs font-medium" style={{ color, background, borderRadius: 20 }}>{children}</span>;
}

function requestUrl(base, version, offset) {
  const params = new URLSearchParams();
  if (version) params.set("version", version);
  if (offset) params.set("assignment_offset", String(offset));
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

export function useRegistry(url, operationsUrl, configUrl) {
  const [state, setState] = useState({ data: null, loading: true, error: null, version: null, offset: 0 });
  const requestId = useRef(0);
  const operations = useOperations(operationsUrl);
  const [direct, setDirect] = useState({ token: null, running: null, error: null });

  useEffect(() => {
    if (operationsUrl || !configUrl) return;
    fetch(configUrl.replace(/\/api\/config$/, "/api/setup/token"), { credentials: "same-origin" })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((body) => setDirect((current) => ({ ...current, token: body.setupToken })))
      .catch(() => setDirect((current) => ({ ...current, error: "Cluster controls are unavailable." })));
  }, [configUrl, operationsUrl]);

  const load = React.useCallback(async (version = state.version, offset = state.offset) => {
    const activeRequest = ++requestId.current;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(requestUrl(url, version, offset), {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = normalizeRegistryPayload(await response.json());
      if (activeRequest !== requestId.current) return false;
      setState({ data, loading: false, error: null, version: data.selectedVersion?.versionId || null, offset });
      return true;
    } catch (_error) {
      if (activeRequest !== requestId.current) return false;
      setState((current) => ({ ...current, loading: false, error: "Registry data is temporarily unavailable." }));
      return false;
    }
  }, [state.offset, state.version, url]);

  useEffect(() => {
    load(null, 0);
    return () => { requestId.current += 1; };
  }, [url]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = React.useCallback(async (kind, parameters) => {
    let result;
    if (operationsUrl) {
      result = await operations.run(kind, parameters);
    } else {
      const action = kind.replace(/^cluster_/, "");
      const payload = {
        strategy: parameters.strategy,
        targetWorkload: parameters.target_workload,
        modelPath: parameters.model_path || undefined,
        versionId: parameters.version,
        expectedGeneration: parameters.expected_generation,
        clusterId: parameters.cluster_id,
        displayName: parameters.display_name,
        cutoff: parameters.cutoff || undefined,
        lookbackDays: parameters.lookback_days,
      };
      setDirect((current) => ({ ...current, running: kind, error: null }));
      try {
        const response = await fetch(`${configUrl.replace(/\/api\/config$/, "")}/api/clusters/${action}`, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-Verdict-Setup": direct.token },
          body: JSON.stringify(payload),
        });
        result = await response.json();
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      } catch (failure) {
        setDirect((current) => ({ ...current, error: failure.message || "Cluster action could not be completed." }));
        await load(state.version, 0);
        return false;
      } finally {
        setDirect((current) => ({ ...current, running: null }));
      }
    }
    if (result) await load(result.registry_version || result.versionId || state.version, 0);
    return Boolean(result);
  }, [configUrl, direct.token, load, operations, operationsUrl, state.version]);

  return {
    ...state,
    load,
    run,
    operations: {
      available: Boolean(operationsUrl ? operations.data?.csrfToken : direct.token),
      running: operationsUrl ? operations.running : direct.running,
      jobs: operations.data?.jobs || [],
      error: operations.error || direct.error,
    },
  };
}

function ActionButton({ children, disabled, onClick, running }) {
  return (
    <button onClick={onClick} disabled={disabled || running} className="px-3 py-2 border text-xs"
      style={{ borderColor: disabled ? COLOR.border : COLOR.accent, color: disabled ? COLOR.faint : COLOR.accent, opacity: running ? 0.6 : 1 }}>
      {running ? "Running…" : children}
    </button>
  );
}

function FitPreviewForm({ operations, running, strategy, setStrategy, targetWorkload, setTargetWorkload, modelPath, setModelPath, onRun }) {
  const [lookbackDays, setLookbackDays] = useState(90);
  const [cutoff, setCutoff] = useState("");
  return (
    <div className="p-3" style={{ background: COLOR.panel2 }}>
      <div className="text-xs font-mono" style={{ color: COLOR.faint }}>ANALYZE HISTORICAL TRACES</div>
      <div className="text-xs mt-2" style={{ color: COLOR.sub }}>Verdict ends the range at your latest eligible trace and analyzes the preceding 90 days.</div>
      <div className="flex flex-wrap gap-2 mt-2">
        <select aria-label="Fit strategy" value={strategy} onChange={(event) => setStrategy(event.target.value)} className="px-2 py-2 border text-xs" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }}>
          <option value="semantic">Semantic clusters (experimental)</option><option value="explicit">Captured intent labels</option><option value="hybrid">Intent labels with semantic fallback (experimental)</option>
        </select>
        <input aria-label="Optional workload filter" value={targetWorkload} onChange={(event) => setTargetWorkload(event.target.value)} placeholder="All eligible traces" maxLength={64} className="px-2 py-2 border text-xs min-w-40" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }} />
        <ActionButton disabled={!operations?.available} running={running === "cluster_fit"} onClick={() => onRun("cluster_fit", { strategy, target_workload: targetWorkload || null, model_path: modelPath || null, cutoff: cutoff ? new Date(cutoff).toISOString() : null, lookback_days: lookbackDays })}>Analyze historical traces</ActionButton>
      </div>
      <details className="mt-3 text-xs" style={{ color: COLOR.sub }}>
        <summary className="cursor-pointer">Advanced range and model settings</summary>
        <div className="grid sm:grid-cols-2 gap-2 mt-3">
          <label>Lookback days<input aria-label="Cluster lookback days" type="number" min="1" value={lookbackDays} onChange={(event) => setLookbackDays(Number(event.target.value))} className="block w-full mt-1 px-2 py-2 border" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }} /></label>
          <label>Optional range end<input aria-label="Cluster range end" type="datetime-local" value={cutoff} onChange={(event) => setCutoff(event.target.value)} className="block w-full mt-1 px-2 py-2 border" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }} /></label>
        </div>
        {strategy !== "explicit" && <label className="block mt-3">Optional MiniLM directory override<input aria-label="Semantic model path" value={modelPath} onChange={(event) => setModelPath(event.target.value)} placeholder="Server auto-detects the pinned model" className="block w-full mt-1 px-2 py-2 border" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }} /><span className="block mt-1" style={{ color: COLOR.faint }}>An approved override is retained for later assignment by this cluster version.</span></label>}
      </details>
    </div>
  );
}

function ReadinessTerm({ label, value }) {
  const known = typeof value === "boolean";
  const color = value === true ? COLOR.green : value === false ? COLOR.red : COLOR.faint;
  return (
    <div className="p-3" style={{ background: COLOR.panel2 }}>
      <div className="text-xs font-mono" style={{ color: COLOR.faint }}>{label}</div>
      <div className="text-sm mt-1 flex items-center gap-1.5" style={{ color }}>
        {value === true ? <CheckCircle2 size={13} /> : value === false ? <AlertTriangle size={13} /> : <Clock size={13} />}
        {known ? value ? "Pass" : "Fail" : "Unavailable"}
      </div>
    </div>
  );
}

function warningText(warning) {
  if (warning === "fragmented_semantic_space") return "Semantic traffic is split across many small clusters; inspect representatives before refitting.";
  if (warning === "oversized_semantic_cluster") return "This semantic cluster contains more than 30% of assigned traffic; inspect for collapsed intents before activation.";
  return warning.replaceAll("_", " ");
}

function ReadinessEstimate({ readiness }) {
  if (readiness.status === "ready") return <span style={{ color: COLOR.green }}>Ready at the default n={readiness.floor} floor</span>;
  if (readiness.status === "unavailable") return <span style={{ color: COLOR.faint }}>Unavailable — no independent session IDs in these windows</span>;
  const estimate = readiness.estimatedDaysToReady == null
    ? "time estimate unavailable"
    : `roughly ${readiness.estimatedDaysToReady} more day${readiness.estimatedDaysToReady === 1 ? "" : "s"}`;
  return <span style={{ color: COLOR.amber }}>Needs {readiness.remainingBaseline} baseline / {readiness.remainingCurrent} current conversations · {estimate}</span>;
}

export function RegistryView({ data, operations, onRun, onVersion, onPage, onRefresh, traceCount = 0, onMonitor = null, onSetup = null, configUrl = null }) {
  const [strategy, setStrategy] = useState("explicit");
  const [targetWorkload, setTargetWorkload] = useState("");
  const [modelPath, setModelPath] = useState("");
  const [renameCluster, setRenameCluster] = useState("");
  const [renameName, setRenameName] = useState("");

  if (!data || data.status === "unavailable") {
    return (
      <Panel className="p-6">
        <div className="flex items-start gap-3"><AlertTriangle size={18} style={{ color: COLOR.amber }} />
          <div><div className="font-semibold">Versioned registry unavailable</div>
            <div className="text-sm mt-1" style={{ color: COLOR.sub }}>This store predates the versioned cluster tables or has not installed them. Captured model calls remain available in Investigation; versioned cluster controls require an upgraded store.</div></div>
        </div>
      </Panel>
    );
  }
  if (data.status === "empty") {
    return <Panel className="p-6"><div className="font-semibold">Group the evidence you have</div>
      {traceCount === 0 ? <><div className="text-sm mt-2" style={{ color: COLOR.sub }}>No genuine model calls are captured. Recorded agent names and sources remain available in Groups and Investigation; trace clustering needs model-call evidence.</div>{onSetup && <button type="button" onClick={onSetup} className="underline text-sm mt-3" style={{ color: COLOR.green }}>Set up model-call capture</button>}</>
        : <><div className="text-sm mt-2" style={{ color: COLOR.sub }}>{traceCount.toLocaleString()} captured model calls. Start with all calls or provider/model groups in Model drift setup. A custom intent registry is optional and needs reviewed examples before activation.</div>
          {onMonitor && <button type="button" onClick={onMonitor} className="underline text-sm mt-3" style={{ color: COLOR.green }}>Review provider/model monitoring</button>}
          <details className="mt-5"><summary className="cursor-pointer text-sm">Create a reviewed intent registry</summary><div className="mt-3 text-xs" style={{ color: COLOR.sub }}>Captured intent labels work without embeddings. Semantic fitting needs usable prompt evidence and an approved local model; inspect coverage, outliers, and representatives before activation.</div><FitPreviewForm operations={operations} running={operations?.running} strategy={strategy} setStrategy={setStrategy} targetWorkload={targetWorkload} setTargetWorkload={setTargetWorkload} modelPath={modelPath} setModelPath={setModelPath} onRun={onRun} /></details></>}
    </Panel>;
  }

  const selected = data.selectedVersion;
  const experimental = selected.strategyStatus.experimental;
  const active = selected.active;
  const activeVersion = data.versions.find((version) => version.active);
  const activatedBefore = data.activationHistory;
  const running = operations?.running;
  const clusterJobs = (operations?.jobs || []).filter((job) => job.kind.startsWith("cluster_"));
  const renameTarget = data.clusters.find((cluster) => cluster.clusterId === renameCluster);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-mono" style={{ color: COLOR.accent }}>TENANT / {data.tenant}</div>
          <div className="flex flex-wrap items-center gap-2 mt-2">
            <span className="font-semibold">{selected.strategy} registry</span>
            <Pill color={active ? COLOR.green : COLOR.amber} background={active ? COLOR.greenBg : COLOR.amberBg}>{active ? "Active" : "Preview / historical"}</Pill>
            {experimental && <Pill color={COLOR.amber} background={COLOR.amberBg}>Experimental opt-in</Pill>}
          </div>
          <div className="text-xs font-mono mt-2" style={{ color: COLOR.faint }}>{selected.versionId} · generation {data.active.generation}</div>
        </div>
        <div className="flex items-center gap-2">
          <select aria-label="Registry version" value={selected.versionId} onChange={(event) => onVersion(event.target.value)} className="px-3 py-2 border text-xs max-w-64" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }}>
            {data.versions.map((version) => <option key={version.versionId} value={version.versionId}>{version.active ? "Active · " : ""}{version.strategy} · {version.versionId}</option>)}
          </select>
          <button onClick={onRefresh} className="p-2 border" title="Refresh registry" style={{ borderColor: COLOR.border, color: COLOR.sub }}><RefreshCw size={14} /></button>
        </div>
      </div>

      {experimental && (
        <div role="status" className="p-4 border flex items-start gap-3" style={{ borderColor: "#6b5529", background: COLOR.amberBg }}>
          <AlertTriangle size={16} style={{ color: COLOR.amber, flexShrink: 0 }} />
          <div className="text-sm"><span className="font-semibold" style={{ color: COLOR.amber }}>Experimental opt-in.</span> Mechanical activation checks do not establish semantic cluster quality. Review representatives, outliers, coverage, and replay behavior before activation.</div>
        </div>
      )}

      <div className="grid sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>ASSIGNED</div><div className="text-2xl font-semibold mt-2" style={{ color: COLOR.green }}>{data.counts.assigned}</div></Panel>
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>OUTLIERS</div><div className="text-2xl font-semibold mt-2" style={{ color: COLOR.amber }}>{data.counts.outlier}</div></Panel>
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>INELIGIBLE</div><div className="text-2xl font-semibold mt-2" style={{ color: COLOR.faint }}>{data.counts.ineligible}</div></Panel>
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>ACTIVATION CHECKS</div><div className="text-lg font-semibold mt-2" style={{ color: data.readiness.passed ? COLOR.green : COLOR.amber }}>{data.readiness.passed ? "Mechanically passed" : data.readiness.status.replaceAll("_", " ")}</div></Panel>
      </div>

      <Panel className="p-4">
        <div className="font-semibold text-sm">Analyzed range</div>
        <div className="text-sm mt-2" style={{ color: COLOR.sub }}>{selected.preview.candidateCount || 0} eligible traces in the {selected.lookbackDays}-day range ending {selected.cutoff || "at the latest eligible trace"}.</div>
        {!active && <div className="mt-3"><ActionButton disabled={!operations?.available} running={running === "cluster_activate"} onClick={() => onRun("cluster_activate", { version: selected.versionId, expected_generation: data.active.generation, ...(modelPath ? { model_path: modelPath } : {}) })}>Use these clusters</ActionButton></div>}
      </Panel>

      {data.counts.assigned === 0 && data.counts.ineligible > 0 && <div role="status" className="p-4 border flex items-start gap-3" style={{ borderColor: "#6b5529", background: COLOR.amberBg }}><AlertTriangle size={16} style={{ color: COLOR.amber, flexShrink: 0 }} /><div className="text-sm"><span className="font-semibold" style={{ color: COLOR.amber }}>No traces were assigned.</span> This version left {data.counts.ineligible.toLocaleString()} trace(s) ineligible. Explicit grouping only uses a captured <code>verdict.intent_key</code>; choose semantic grouping with an approved local model only when prompt evidence is available, or inspect the member explanations below.</div></div>}

      <Panel className="p-4">
        <div className="font-semibold text-sm flex items-center gap-2"><CheckCircle2 size={15} style={{ color: data.readiness.passed ? COLOR.green : COLOR.amber }} />Mechanical activation checks</div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2 mt-3">
          <ReadinessTerm label="STRUCTURE" value={data.readiness.structural} />
          <ReadinessTerm label="COVERAGE" value={data.readiness.coverage} />
          <ReadinessTerm label="DEFINITION" value={data.readiness.definition} />
          <ReadinessTerm label="MODEL" value={data.readiness.model} />
        </div>
      </Panel>

      <div className="grid lg:grid-cols-2 gap-3">
        <Panel className="p-4">
          <div className="font-semibold text-sm">Frozen version definition</div>
          <div className="grid grid-cols-2 gap-2 mt-3 text-xs">
            <div className="p-3" style={{ background: COLOR.panel2 }}><div style={{ color: COLOR.faint }}>ALGORITHM</div><div className="mt-1 font-mono">{selected.algorithm || "Unavailable"}</div></div>
            <div className="p-3" style={{ background: COLOR.panel2 }}><div style={{ color: COLOR.faint }}>SELECTOR</div><div className="mt-1 font-mono">{selected.selector || "Unavailable"}</div></div>
          </div>
          <div className="mt-3 text-xs" style={{ color: COLOR.sub }}>
            {Object.keys(selected.model).length > 0
              ? `Model ${selected.model.name || "unknown"} · revision ${selected.model.revision || "unknown"}`
              : "No semantic model is used by this explicit version."}
          </div>
          {Object.keys(selected.configuration).length > 0 && <div className="flex flex-wrap gap-2 mt-3">{Object.entries(selected.configuration).slice(0, 12).map(([key, value]) => <Pill key={key}>{key.replaceAll("_", " ")} · {String(value)}</Pill>)}</div>}
        </Panel>
        <Panel className="p-4">
          <div className="font-semibold text-sm flex items-center gap-2"><BarChart3 size={15} style={{ color: COLOR.blue }} />Assigned provider / model distribution</div>
          {data.modelDistribution.length === 0
            ? <div className="text-xs mt-3" style={{ color: COLOR.faint }}>No assigned provider/model evidence is available.</div>
            : <div className="space-y-2 mt-3">{data.modelDistribution.map((item) => <div key={`${item.provider}-${item.model}`} className="flex justify-between gap-3 text-xs"><span>{item.provider} · {item.model}</span><span className="font-mono" style={{ color: COLOR.sub }}>{item.count}</span></div>)}{data.modelDistributionTruncated && <div className="text-xs" style={{ color: COLOR.faint }}>Showing the 20 most common provider/model pairs.</div>}</div>}
        </Panel>
      </div>

      {(data.healthWarnings.length > 0 || selected.preview.warnings?.length > 0) && <Panel className="p-4" style={{ borderColor: "#6b5529" }}><div className="font-semibold text-sm flex items-center gap-2"><AlertTriangle size={15} style={{ color: COLOR.amber }} />Cluster health warnings</div><div className="space-y-2 mt-3 text-xs" style={{ color: COLOR.sub }}>{[...data.healthWarnings, ...(selected.preview.warnings || [])].map((warning, index) => <div key={`${warning}-${index}`}>{warningText(warning)}</div>)}</div></Panel>}

      <Panel className="p-4">
        <div className="flex items-center gap-2 font-semibold text-sm"><GitBranch size={15} style={{ color: COLOR.blue }} />Run another analysis</div>
        {!operations?.available && <div className="text-xs mt-2" style={{ color: COLOR.faint }}>Read-only dashboard. An authenticated host operations adapter is required for mutations.</div>}
        <div className="grid lg:grid-cols-2 gap-4 mt-3">
          <FitPreviewForm operations={operations} running={running} strategy={strategy} setStrategy={setStrategy} targetWorkload={targetWorkload} setTargetWorkload={setTargetWorkload} modelPath={modelPath} setModelPath={setModelPath} onRun={onRun} />
          <details className="p-3" style={{ background: COLOR.panel2 }}>
            <summary className="text-xs font-mono cursor-pointer" style={{ color: COLOR.faint }}>ADVANCED VERSION CONTROLS</summary>
            <div className="flex flex-wrap gap-2 mt-2">
              <ActionButton disabled={!operations?.available || !activeVersion} running={running === "cluster_refit"} onClick={() => onRun("cluster_refit", activeVersion.strategy === "explicit" ? {} : { model_path: modelPath || null })}>Refit active</ActionButton>
              <ActionButton disabled={!operations?.available || !selected} running={running === "cluster_validate"} onClick={() => onRun("cluster_validate", { version: selected.versionId, ...(modelPath ? { model_path: modelPath } : {}) })}>Validate version</ActionButton>
              <ActionButton disabled={!operations?.available || !selected} running={running === "cluster_replay"} onClick={() => onRun("cluster_replay", { version: selected.versionId, ...(modelPath ? { model_path: modelPath } : {}) })}>Replay assignments</ActionButton>
              <ActionButton disabled={!operations?.available || active || !activatedBefore} running={running === "cluster_rollback"} onClick={() => onRun("cluster_rollback", { version: selected.versionId, expected_generation: data.active.generation, ...(modelPath ? { model_path: modelPath } : {}) })}>Rollback to version</ActionButton>
            </div>
          </details>
        </div>
        <div className="p-3 mt-3" style={{ background: COLOR.panel2 }}>
          <div className="text-xs font-mono" style={{ color: COLOR.faint }}>DISPLAY LABEL</div>
          <div className="flex flex-wrap gap-2 mt-2">
            <select aria-label="Cluster to rename" value={renameCluster} onChange={(event) => { const id = event.target.value; setRenameCluster(id); setRenameName(data.clusters.find((item) => item.clusterId === id)?.displayName || ""); }} className="px-2 py-2 border text-xs" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }}>
              <option value="">Select cluster</option>{data.clusters.map((cluster) => <option key={cluster.clusterId} value={cluster.clusterId}>{cluster.displayName}</option>)}
            </select>
            <input aria-label="Cluster display name" value={renameName} onChange={(event) => setRenameName(event.target.value)} maxLength={80} className="px-2 py-2 border text-xs min-w-48" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }} />
            <ActionButton disabled={!operations?.available || !renameTarget || !renameName} running={running === "cluster_rename"} onClick={() => onRun("cluster_rename", { cluster_id: renameCluster, display_name: renameName })}>Rename cluster</ActionButton>
          </div>
          {renameTarget && configUrl && <EvidenceExplain key={`${selected.versionId}:${renameCluster}`}
            root={configUrl.replace(/\/api\/config$/, "")} scope="cluster"
            versionId={selected.versionId} clusterId={renameCluster}
            draftKind="cluster_description" draftActionLabel="Use proposed label in rename form"
            onApplyDraft={(draft) => setRenameName(draft.displayName)} />}
          {renameTarget && <p className="text-xs mt-2" style={{ color: COLOR.sub }}>AI suggestions use bounded representatives when available. Inspect cluster members before renaming or activating.</p>}
        </div>
      </Panel>

      <Panel className="overflow-hidden">
        <div className="px-4 py-3 border-b flex items-center gap-2" style={{ borderColor: COLOR.border }}><Layers size={15} style={{ color: COLOR.accent }} /><span className="font-semibold text-sm">Version clusters</span>{data.clusterDetailsTruncated && <span className="ml-auto text-xs" style={{ color: COLOR.faint }}>Detailed evidence for the 20 highest-volume clusters</span>}</div>
        <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr style={{ color: COLOR.faint }}><th className="text-left p-3">Label</th><th className="text-left p-3">Membership</th><th className="text-left p-3">Independent-conversation readiness</th><th className="text-left p-3">Representative redacted prompts</th></tr></thead><tbody>{data.clusters.map((cluster) => <tr key={cluster.clusterId} className="border-t align-top" style={{ borderColor: COLOR.border }}><td className="p-3"><div>{cluster.displayName}</div><div className="text-xs font-mono" style={{ color: COLOR.faint }}>{cluster.clusterId}</div><div className="mt-2"><Pill>{cluster.kind} · {cluster.lifecycle}</Pill></div></td><td className="p-3 text-xs" style={{ color: COLOR.sub }}><div>{cluster.assignedCount} assigned</div><div className="mt-1">{cluster.kind === "explicit" ? `Exact key: ${cluster.explicitKey}` : `Radius ${cluster.radius == null ? "unavailable" : cluster.radius.toFixed(3)}`}</div>{cluster.modelDistribution.map((item) => <div key={`${item.provider}-${item.model}`} className="mt-1">{item.provider} · {item.model} · {item.count}</div>)}{cluster.modelDistributionTruncated && <div className="mt-1" style={{ color: COLOR.faint }}>Top five provider/model pairs shown.</div>}{cluster.warnings.map((warning) => <div key={warning} className="mt-2" style={{ color: COLOR.amber }}>{warningText(warning)}</div>)}</td><td className="p-3 text-xs min-w-64">{cluster.detailsAvailable ? <><div>Baseline {cluster.conversationReadiness.baseline}/{cluster.conversationReadiness.floor} · Current {cluster.conversationReadiness.current}/{cluster.conversationReadiness.floor}</div><div className="mt-2"><ReadinessEstimate readiness={cluster.conversationReadiness} /></div></> : <span style={{ color: COLOR.faint }}>Detail omitted from this bounded view.</span>}</td><td className="p-3 text-xs min-w-72">{!cluster.detailsAvailable ? <span style={{ color: COLOR.faint }}>Detail omitted from this bounded view.</span> : cluster.representatives.length === 0 ? <span style={{ color: COLOR.faint }}>Content not captured or no bounded prompt is available.</span> : <div className="space-y-2">{cluster.representatives.map((representative) => <div key={representative.traceId} className="p-2" style={{ background: COLOR.panel2 }}><div style={{ color: COLOR.text }}>{representative.prompt}</div><div className="mt-1 font-mono" style={{ color: COLOR.faint }}>{representative.provider} · {representative.model}</div></div>)}{cluster.representativesTruncated && <div style={{ color: COLOR.faint }}>Three bounded representatives shown.</div>}</div>}</td></tr>)}</tbody></table></div>
        <div className="px-4 py-3 border-t text-xs" style={{ borderColor: COLOR.border, color: COLOR.faint }}>Readiness is a planning estimate as of the selected version cutoff: 7-day baseline, 1-day gap, and 1-day current window. It counts distinct strict-UTF-8 session IDs with 1–256 bytes and no NUL at the default n={data.trafficWindow.conversationFloor} floor; it is not an activation or drift result.</div>
      </Panel>

      {data.reasons.length > 0 && <Panel className="p-4"><div className="font-semibold text-sm">Outlier and ineligible reasons</div><div className="flex flex-wrap gap-2 mt-3">{data.reasons.map((reason) => <Pill key={`${reason.status}-${reason.reason}`} color={reason.status === "outlier" ? COLOR.amber : COLOR.sub}>{reason.reason.replaceAll("_", " ")} · {reason.count}</Pill>)}</div></Panel>}

      <Panel className="overflow-hidden">
        <div className="px-4 py-3 border-b flex items-center gap-2" style={{ borderColor: COLOR.border }}><Activity size={15} style={{ color: COLOR.blue }} /><span className="font-semibold text-sm">Member explanations</span><span className="ml-auto text-xs" style={{ color: COLOR.faint }}>{data.page.shown} of {data.page.available}</span></div>
        <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr style={{ color: COLOR.faint }}><th className="text-left p-3">Trace</th><th className="text-left p-3">Result</th><th className="text-left p-3">Explanation</th><th className="text-left p-3">Origin</th></tr></thead><tbody>{data.assignments.map((assignment) => <tr key={assignment.traceId} className="border-t" style={{ borderColor: COLOR.border }}><td className="p-3 font-mono text-xs">{assignment.traceId}</td><td className="p-3"><Pill color={assignment.status === "assigned" ? COLOR.green : assignment.status === "outlier" ? COLOR.amber : COLOR.sub}>{assignment.status}</Pill></td><td className="p-3 text-xs" style={{ color: COLOR.sub }}>{assignmentExplanation(assignment, data.clusters)}</td><td className="p-3 text-xs">{assignment.origin}</td></tr>)}</tbody></table></div>
        <div className="px-4 py-3 border-t flex justify-end gap-2" style={{ borderColor: COLOR.border }}><button disabled={data.page.offset === 0} onClick={() => onPage(Math.max(0, data.page.offset - data.page.limit))} className="px-3 py-2 border text-xs" style={{ borderColor: COLOR.border }}>Previous</button><button disabled={!data.page.truncated} onClick={() => onPage(data.page.offset + data.page.limit)} className="px-3 py-2 border text-xs" style={{ borderColor: COLOR.border }}>Next</button></div>
      </Panel>

      <Panel className="p-4">
        <div className="font-semibold text-sm">Cluster job state</div>
        {clusterJobs.length === 0 ? <div className="text-xs mt-2" style={{ color: COLOR.faint }}>No registry jobs have completed in this workspace.</div> : <div className="space-y-2 mt-3">{clusterJobs.slice(0, 10).map((job) => <div key={job.id} className="p-3 flex flex-wrap items-center gap-2" style={{ background: COLOR.panel2 }}><span>{job.kind.replaceAll("_", " ")}</span><span className="text-xs" style={{ color: COLOR.faint }}>{job.startedAt}</span><Pill color={job.status === "succeeded" ? COLOR.green : job.status === "failed" ? COLOR.red : COLOR.amber}>{job.status}</Pill></div>)}</div>}
      </Panel>
    </div>
  );
}

function RecordedExecutionSummary({ execution, onOpenRun }) {
  if (!execution?.scope?.sampledRuns) return null;
  const labels = { completed: "completed", failed: "failed", timed_out: "timed out",
    cancelled: "cancelled", unfinalized: "unfinalized" };
  return <section className="border p-5 mb-3" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}>
    <h2 className="text-lg font-semibold">Recorded execution status</h2>
    <p className="text-sm mt-2" style={{ color: COLOR.sub }}>Latest {execution.scope.sampledRuns.toLocaleString()} captured Agent Runs by start time. An unfinalized run has no recorded end or terminal status. These counts describe capture, not task success or a statistical alert.</p>
    {execution.scope.truncated && <p className="text-xs mt-2" style={{ color: COLOR.amber }}>Showing the latest {execution.scope.sampledRuns.toLocaleString()} runs; older runs are omitted.</p>}
    <p className="text-sm mt-3" style={{ color: COLOR.sub }}>{Object.entries(labels).map(([key, label]) => `${execution.totals[key] || 0} ${label}`).join(" · ")}</p>
    <div className="grid lg:grid-cols-2 gap-3 mt-3">{execution.groups.map((group) => <div key={[group.sourceKind, group.serviceName, group.environment, group.agentName].join("/")} className="border p-3 text-sm" style={{ borderColor: COLOR.border }}>
      <strong>{[group.sourceKind, group.serviceName || "service not recorded", group.environment || "environment not recorded", group.agentName || "agent not recorded"].join(" / ")}</strong>
      <p className="mt-1" style={{ color: COLOR.sub }}>{Object.entries(labels).map(([key, label]) => `${group.counts[key] || 0} ${label}`).join(" · ")}</p>
      <div className="flex flex-wrap gap-3 mt-2">{Object.entries(labels).flatMap(([key, label]) => (group.examples[key] || []).map((runId, index) => <button key={`${key}:${runId}`} type="button" className="underline text-xs" style={{ color: COLOR.accent }} onClick={() => onOpenRun?.(runId)}>Inspect {label} run {index + 1}</button>))}</div>
    </div>)}</div>
    {execution.groupsTruncated && <p className="text-xs mt-3" style={{ color: COLOR.amber }}>Only the 20 largest recorded agent groups are shown.</p>}
  </section>;
}

export function RunOutcomeSummary({ data, onOpenRun, onRuns }) {
  if (data.status === "migration_required") return <section className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}><h2 className="text-lg font-semibold">Recorded tool-result coverage by run</h2><p className="text-sm mt-2" style={{ color: COLOR.amber }}>This store needs a writable Verdict storage upgrade before tool-result coverage can load. No outcome counts are shown.</p></section>;
  const execution = RecordedExecutionSummary({ execution: data.execution, onOpenRun });
  if (data.status === "event_limit") return <>{execution}<section className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}><h2 className="text-lg font-semibold">Recorded tool-result coverage by run</h2><p className="text-sm mt-2" style={{ color: COLOR.amber }}>The bounded preview reached its relevant-event limit. No outcome counts are shown for this sample.</p></section></>;
  if (data.status === "no_terminal_runs") {
    const sampledRuns = data.execution?.scope?.sampledRuns || 0;
    const exampleRun = data.execution?.groups?.find((group) => group.examples?.unfinalized?.length)
      ?.examples.unfinalized[0];
    return <section className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}>
      <h2 className="text-lg font-semibold">Run outcome coverage</h2>
      {sampledRuns ? <>
        <p className="text-sm mt-2" style={{ color: COLOR.sub }}>{sampledRuns.toLocaleString()} recent recorded runs have no terminal status with an end time. These histories can still be inspected. For agents you control, record a terminal status and end time to compare run outcomes.</p>
        {data.execution.scope.truncated && <p className="text-xs mt-2" style={{ color: COLOR.amber }}>This is the latest {sampledRuns.toLocaleString()} runs; older runs are omitted.</p>}
        <div className="flex flex-wrap gap-4 mt-3">
          {onRuns && <button type="button" onClick={onRuns} className="text-sm underline" style={{ color: COLOR.accent }}>Browse recorded runs →</button>}
          {exampleRun && onOpenRun && <button type="button" onClick={() => onOpenRun(exampleRun)} className="text-sm underline" style={{ color: COLOR.accent }}>Inspect example run →</button>}
        </div>
      </> : <p className="text-sm mt-2" style={{ color: COLOR.sub }}>No Agent Runs have been captured yet. Capture a run to inspect its recorded actions.</p>}
    </section>;
  }
  const totals = data.totals;
  return <>{execution}<section className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}>
    <h2 className="text-lg font-semibold">Recorded tool-result coverage by run</h2>
    <p className="text-sm mt-2" style={{ color: COLOR.sub }}>Recorded tool actions and capture gaps across the latest {data.scope.sampledTerminalRuns.toLocaleString()} terminal runs. Run-status and test failures appear in Monitoring. This is descriptive evidence, not task success or a statistical alert.</p>
    {data.scope.truncated && <p className="text-xs mt-2" style={{ color: COLOR.amber }}>Showing the latest {data.scope.sampledTerminalRuns.toLocaleString()} terminal runs; older runs are omitted.</p>}
    <div className="grid sm:grid-cols-3 gap-3 mt-4">
      <div className="p-3" style={{ background: COLOR.redBg }}><strong>{totals.recordedFailure.toLocaleString()} recorded tool-result failures</strong><p className="text-xs mt-1" style={{ color: COLOR.sub }}>Explicit failed or timed-out tool results</p></div>
      <div className="p-3" style={{ background: COLOR.panel2 }}><strong>{totals.observedWithoutFailure.toLocaleString()} observed without recorded failure</strong><p className="text-xs mt-1" style={{ color: COLOR.sub }}>A completed result with no unclear evidence. When calls are recorded, each has a matching result.</p></div>
      <div className="p-3" style={{ background: COLOR.amberBg }}><strong>{totals.unclear.toLocaleString()} unclear</strong><p className="text-xs mt-1" style={{ color: COLOR.sub }}>Missing, unmatched, unknown, cancelled, or partially captured tool outcomes</p></div>
    </div>
    {totals.conflicts > 0 && <p className="text-xs mt-3" style={{ color: COLOR.amber }}>{totals.conflicts.toLocaleString()} result status and error flag conflict{totals.conflicts === 1 ? "" : "s"}; the explicit failure signal takes precedence.</p>}
    <div className="grid lg:grid-cols-2 gap-3 mt-4">{data.groups.map((group) => <div key={[group.sourceKind, group.serviceName, group.environment, group.agentName].join("/")} className="border p-3 text-sm" style={{ borderColor: COLOR.border }}>
      <strong>{[group.sourceKind, group.serviceName || "service not recorded", group.environment || "environment not recorded", group.agentName || "agent not recorded"].join(" / ")}</strong>
      <p className="mt-1" style={{ color: COLOR.sub }}>{group.recordedFailure} recorded tool failures · {group.observedWithoutFailure} observed without recorded failure · {group.unclear} unclear / {group.terminalRuns} terminal runs</p>
      {group.comparison && <div className="mt-2 text-xs" style={{ color: COLOR.sub }}>
        <div>Earlier {group.comparison.earlier.terminalRuns} runs: {group.comparison.earlier.recordedFailure} recorded tool failure{group.comparison.earlier.recordedFailure === 1 ? "" : "s"}, {group.comparison.earlier.unclear} unclear, {group.comparison.earlier.observedWithoutFailure} observed without recorded failure.</div>
        <div>Newer {group.comparison.newer.terminalRuns} runs: {group.comparison.newer.recordedFailure} recorded tool failure{group.comparison.newer.recordedFailure === 1 ? "" : "s"}, {group.comparison.newer.unclear} unclear, {group.comparison.newer.observedWithoutFailure} observed without recorded failure.</div>
        <p>Descriptive split only, ordered by recorded run end time. This is not a saved monitor or a statistical alert.</p>
      </div>}
      <div className="flex flex-wrap gap-3 mt-2">{group.examples.recordedFailure.map((runId, index) => <button key={runId} type="button" className="underline text-xs" style={{ color: COLOR.red }} onClick={() => onOpenRun?.(runId)}>Inspect recorded failure {index + 1}</button>)}{group.examples.observedWithoutFailure.map((runId, index) => <button key={runId} type="button" className="underline text-xs" style={{ color: COLOR.blue }} onClick={() => onOpenRun?.(runId)}>Inspect observed run {index + 1}</button>)}{group.examples.unclear.map((runId, index) => <button key={runId} type="button" className="underline text-xs" style={{ color: COLOR.amber }} onClick={() => onOpenRun?.(runId)}>Inspect unclear run {index + 1}</button>)}</div>
    </div>)}</div>
    {data.groupsTruncated && <p className="text-xs mt-3" style={{ color: COLOR.amber }}>Only the 20 largest recorded agent groups are shown.</p>}
  </section></>;
}

export function RunOutcomePreview({ configUrl, onOpenRun, onRuns }) {
  const [state, setState] = useState({ data: null, error: false });
  const [refreshKey, setRefreshKey] = useState(0);
  const endpoint = configUrl?.replace(/\/api\/config$/, "/api/agent-run-outcomes");
  useEffect(() => {
    if (!endpoint) return undefined;
    let active = true;
    fetch(endpoint, { credentials: "same-origin", cache: "no-store" })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("unavailable")))
      .then((data) => { if (active) setState({ data, error: false }); })
      .catch(() => { if (active) setState({ data: null, error: true }); });
    return () => { active = false; };
  }, [endpoint, refreshKey]);
  if (!endpoint) return null;
  const refresh = () => { setState({ data: null, error: false }); setRefreshKey((key) => key + 1); };
  return <div className="space-y-2">
    <button type="button" className="text-xs underline" style={{ color: COLOR.accent }} onClick={refresh}>{state.error ? "Retry run outcomes" : "Refresh run outcomes"}</button>
    {state.error ? <div role="status" className="border p-4 text-sm" style={{ borderColor: COLOR.border, color: COLOR.amber }}>Run outcome coverage could not be loaded.</div>
      : !state.data ? <div role="status" className="border p-4 text-sm" style={{ borderColor: COLOR.border, color: COLOR.sub }}>Loading recorded run outcomes…</div>
        : <RunOutcomeSummary data={state.data} onOpenRun={onOpenRun} onRuns={onRuns} />}
  </div>;
}

export function Registry({ url, operationsUrl, configUrl, traceCount = 0, sourceGroups = [], sourceGroupsTruncated = false,
  agentGroups = [], agentGroupsTruncated = false, modelRows = [], modelRowsAvailable = 0,
  onMonitor = null, onSetup = null, onRuns = null, onPlan = null, onOpenRun = null,
  onSourceGroup = null, onAgentGroup = null }) {
  const state = useRegistry(url, operationsUrl, configUrl);
  const sourceTotal = sourceGroups.reduce((sum, group) => sum + Number(group.runs || 0), 0);
  const registry = <RegistryView data={state.data} operations={state.operations} onRun={state.run}
    traceCount={traceCount} onMonitor={onMonitor} onSetup={onSetup} configUrl={configUrl}
    onVersion={(version) => state.load(version, 0)} onPage={(offset) => state.load(state.version, offset)}
    onRefresh={() => state.load(state.version, state.offset)} />;
  return (
    <div className="space-y-5">
      <div className="verdict-groups-grid">
        <section className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}>
          <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-lg font-semibold">Agent history: ready-made groups</h2><Pill color={COLOR.green} background={COLOR.greenBg}>OBSERVED SOURCE DATA</Pill></div>
          <p className="text-sm mt-2" style={{ color: COLOR.sub }}>These groups use recorded source labels. They are not inferred intent clusters.</p>
          {sourceGroups.length ? <>
            <div className="flex h-3 overflow-hidden mt-5 rounded-full" aria-hidden="true">{sourceGroups.map((group, index) => <span key={group.sourceKind} style={{ width: `${100 * Number(group.runs || 0) / Math.max(1, sourceTotal)}%`, background: index % 2 ? "#75aaf5" : COLOR.accent }} />)}</div>
            <div className="mt-4 space-y-2">{sourceGroups.map((group, index) => <button type="button" key={group.sourceKind} onClick={() => onSourceGroup?.(group.sourceKind)} className="flex items-center gap-3 text-sm text-left w-full p-2 rounded hover:bg-[#1b2b24] focus-visible:outline" aria-label={`Inspect ${group.sourceKind} runs`}><span className="h-2 w-2 rounded-full" style={{ background: index % 2 ? "#75aaf5" : COLOR.accent }} /><strong>{group.sourceKind}</strong><span style={{ color: COLOR.sub }}>{Number(group.runs || 0).toLocaleString()} recorded run{Number(group.runs || 0) === 1 ? "" : "s"}</span><span className="ml-auto" style={{ color: COLOR.accent }}>→</span></button>)}</div>
            {sourceGroupsTruncated && <p className="text-xs mt-3" style={{ color: COLOR.amber }}>Only the first source groups are shown. Browse captured runs for more.</p>}
            {agentGroups.length > 0 && <div className="border-t mt-5 pt-4" style={{ borderColor: COLOR.border }}><h3 className="font-semibold">By recorded agent name</h3><div className="grid sm:grid-cols-2 gap-2 mt-3">{agentGroups.map((group) => <button type="button" key={group.agentName} onClick={() => onAgentGroup?.(group.agentName)} className="text-sm text-left p-2 rounded hover:bg-[#1b2b24] focus-visible:outline" aria-label={`Inspect ${group.agentName} runs`}><strong>{group.agentName}</strong><span className="ml-2" style={{ color: COLOR.sub }}>{Number(group.runs || 0).toLocaleString()} run{Number(group.runs || 0) === 1 ? "" : "s"}</span><span className="ml-2" style={{ color: COLOR.accent }}>→</span></button>)}</div>{agentGroupsTruncated && <p className="text-xs mt-2" style={{ color: COLOR.amber }}>Showing the 16 most frequent agent names.</p>}</div>}
            {onRuns && <button type="button" onClick={onRuns} className="text-sm underline mt-5" style={{ color: COLOR.accent }}>Browse all recorded runs →</button>}
          </> : <p className="text-sm mt-5" style={{ color: COLOR.sub }}>No agent runs have been captured yet. Source groups appear after capture.</p>}
        </section>
        <aside className="border p-5" style={{ borderColor: COLOR.border, background: "#211e18", borderRadius: 12 }}>
          <h2 className="text-lg font-semibold">When to use clusters</h2>
          <p className="text-sm mt-3" style={{ color: COLOR.sub }}>{traceCount ? "Start with the captured provider and model fields. Fit semantic intent clusters only when a specific behavior needs its own watch and usable prompt evidence is available." : "Agent history alone does not provide genuine provider-call traces. Source groups work now; semantic intent fitting needs eligible model-call evidence."}</p>
          {onPlan && <button type="button" onClick={onPlan} className="text-sm underline mt-5" style={{ color: COLOR.accent }}>Review recommended checks →</button>}
        </aside>
      </div>
      <RunOutcomePreview configUrl={configUrl} onOpenRun={onOpenRun} onRuns={onRuns} />
      {traceCount > 0 && <section className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}>
        <h2 className="text-lg font-semibold">Captured provider and model groups</h2>
        <p className="text-sm mt-2" style={{ color: COLOR.sub }}>These are recorded model calls in the reporting period. A group is not a drift alert; preview its eligible reference and current cohorts first.</p>
        {modelRows.length ? <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-4">{modelRows.map((row) => <div key={`${row.provider}/${row.model}`} className="border p-3 text-sm" style={{ borderColor: COLOR.border }}><strong className="break-all">{row.provider} / {row.model === "<synthetic>" ? "Synthetic model marker" : row.model || "Model not recorded"}</strong><div style={{ color: COLOR.sub }}>{Number(row.calls || 0).toLocaleString()} captured calls</div><div style={{ color: COLOR.sub }}>{Number(row.failedCalls || 0).toLocaleString()} calls with recorded provider errors</div><div style={{ color: COLOR.sub }}>{Number(row.tokenKnownCalls || 0).toLocaleString()} of {Number(row.calls || 0).toLocaleString()} calls with token totals</div></div>)}</div> : <p className="text-sm mt-4" style={{ color: COLOR.sub }}>No provider/model rows in the selected reporting period.</p>}
        {modelRowsAvailable > modelRows.length && <p className="text-xs mt-3" style={{ color: COLOR.amber }}>Showing {modelRows.length} of {modelRowsAvailable} model groups.</p>}
        {onMonitor && <button type="button" onClick={onMonitor} className="text-sm underline mt-4" style={{ color: COLOR.accent }}>Review model-call comparison →</button>}
      </section>}
      {(state.error || state.operations.error) && <div role="alert" className="p-3 mb-4 border text-sm flex gap-2" style={{ borderColor: COLOR.red, background: COLOR.redBg }}><AlertTriangle size={16} style={{ color: COLOR.red }} />{state.error || state.operations.error}</div>}
      {!state.data && state.loading ? <div className="p-5 text-sm" style={{ color: COLOR.sub }}>Loading optional cluster registry…</div>
        : state.data?.status === "empty" ? <details className="border p-5" style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 12 }}><summary className="cursor-pointer font-semibold">Optional intent cluster registry</summary><div className="mt-4">{registry}</div></details>
          : registry}
    </div>
  );
}
