import React, { useEffect, useRef, useState } from "react";
import {
  Activity, AlertTriangle, BarChart3, CheckCircle2, Clock, GitBranch, Layers, RefreshCw,
} from "lucide-react";

import { useOperations } from "./Operations.jsx";
import { assignmentExplanation, normalizeRegistryPayload } from "./registry-data.js";

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

export function useRegistry(url, operationsUrl) {
  const [state, setState] = useState({ data: null, loading: true, error: null, version: null, offset: 0 });
  const requestId = useRef(0);
  const operations = useOperations(operationsUrl);

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
    const result = await operations.run(kind, parameters);
    if (result) await load(result.registry_version || state.version, 0);
    return Boolean(result);
  }, [load, operations, state.version]);

  return {
    ...state,
    load,
    run,
    operations: {
      available: Boolean(operationsUrl && operations.data?.csrfToken),
      running: operations.running,
      jobs: operations.data?.jobs || [],
      error: operations.error,
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

export function RegistryView({ data, operations, onRun, onVersion, onPage, onRefresh }) {
  const [strategy, setStrategy] = useState("explicit");
  const [targetWorkload, setTargetWorkload] = useState("agent");
  const [renameCluster, setRenameCluster] = useState("");
  const [renameName, setRenameName] = useState("");

  if (!data || data.status === "unavailable") {
    return (
      <Panel className="p-6">
        <div className="flex items-start gap-3"><AlertTriangle size={18} style={{ color: COLOR.amber }} />
          <div><div className="font-semibold">Versioned registry unavailable</div>
            <div className="text-sm mt-1" style={{ color: COLOR.sub }}>This store predates Task 5 or has not installed its additive registry tables. Legacy trace clusters remain visible in Overview and Trace explorer.</div></div>
        </div>
      </Panel>
    );
  }
  if (data.status === "empty") {
    return <Panel className="p-6"><div className="font-semibold">No registry versions yet</div><div className="text-sm mt-1" style={{ color: COLOR.sub }}>Fit a deliberate explicit, semantic, or hybrid preview before activation.</div></Panel>;
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
          <div className="text-sm"><span className="font-semibold" style={{ color: COLOR.amber }}>Experimental opt-in.</span> The frozen semantic holdout failed one dominant-cluster gate: 30.1047% versus the 30.0% maximum. The other 12 preregistered checks passed; this is not a general validated-quality claim.</div>
        </div>
      )}

      <div className="grid sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>ASSIGNED</div><div className="text-2xl font-semibold mt-2" style={{ color: COLOR.green }}>{data.counts.assigned}</div></Panel>
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>OUTLIERS</div><div className="text-2xl font-semibold mt-2" style={{ color: COLOR.amber }}>{data.counts.outlier}</div></Panel>
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>INELIGIBLE</div><div className="text-2xl font-semibold mt-2" style={{ color: COLOR.faint }}>{data.counts.ineligible}</div></Panel>
        <Panel className="p-4"><div className="text-xs font-mono" style={{ color: COLOR.faint }}>READINESS</div><div className="text-lg font-semibold mt-2" style={{ color: data.readiness.passed ? COLOR.green : COLOR.amber }}>{data.readiness.passed ? "Validated" : data.readiness.status.replaceAll("_", " ")}</div></Panel>
      </div>

      <Panel className="p-4">
        <div className="font-semibold text-sm flex items-center gap-2"><CheckCircle2 size={15} style={{ color: data.readiness.passed ? COLOR.green : COLOR.amber }} />Activation readiness</div>
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
        <div className="flex items-center gap-2 font-semibold text-sm"><GitBranch size={15} style={{ color: COLOR.blue }} />Registry controls</div>
        {!operations?.available && <div className="text-xs mt-2" style={{ color: COLOR.faint }}>Read-only dashboard. An authenticated host operations adapter is required for mutations.</div>}
        <div className="grid lg:grid-cols-2 gap-4 mt-3">
          <div className="p-3" style={{ background: COLOR.panel2 }}>
            <div className="text-xs font-mono" style={{ color: COLOR.faint }}>NEW PREVIEW</div>
            <div className="flex flex-wrap gap-2 mt-2">
              <select aria-label="Fit strategy" value={strategy} onChange={(event) => setStrategy(event.target.value)} className="px-2 py-2 border text-xs" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }}>
                <option value="explicit">Explicit (supported)</option><option value="semantic">Semantic (experimental)</option><option value="hybrid">Hybrid (experimental fallback)</option>
              </select>
              <input aria-label="Target workload" value={targetWorkload} onChange={(event) => setTargetWorkload(event.target.value)} maxLength={64} className="px-2 py-2 border text-xs min-w-40" style={{ color: COLOR.text, background: COLOR.panel, borderColor: COLOR.border }} />
              <ActionButton disabled={!operations?.available} running={running === "cluster_fit"} onClick={() => onRun("cluster_fit", { strategy, target_workload: targetWorkload || null })}>Fit preview</ActionButton>
            </div>
          </div>
          <div className="p-3" style={{ background: COLOR.panel2 }}>
            <div className="text-xs font-mono" style={{ color: COLOR.faint }}>VERSION LIFECYCLE</div>
            <div className="flex flex-wrap gap-2 mt-2">
              <ActionButton disabled={!operations?.available || !activeVersion} running={running === "cluster_refit"} onClick={() => onRun("cluster_refit", {})}>Refit active</ActionButton>
              <ActionButton disabled={!operations?.available || active || !data.readiness.passed} running={running === "cluster_activate"} onClick={() => onRun("cluster_activate", { version: selected.versionId, expected_generation: data.active.generation })}>Activate version</ActionButton>
              <ActionButton disabled={!operations?.available || active || !activatedBefore} running={running === "cluster_rollback"} onClick={() => onRun("cluster_rollback", { version: selected.versionId, expected_generation: data.active.generation })}>Rollback to version</ActionButton>
            </div>
          </div>
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

export function Registry({ url, operationsUrl }) {
  const state = useRegistry(url, operationsUrl);
  if (!state.data && state.loading) return <div className="p-8 text-sm" style={{ color: COLOR.sub }}>Loading cluster registry…</div>;
  return (
    <div>
      {(state.error || state.operations.error) && <div role="alert" className="p-3 mb-4 border text-sm flex gap-2" style={{ borderColor: COLOR.red, background: COLOR.redBg }}><AlertTriangle size={16} style={{ color: COLOR.red }} />{state.error || state.operations.error}</div>}
      <RegistryView data={state.data} operations={state.operations} onRun={state.run}
        onVersion={(version) => state.load(version, 0)} onPage={(offset) => state.load(state.version, offset)}
        onRefresh={() => state.load(state.version, state.offset)} />
    </div>
  );
}
