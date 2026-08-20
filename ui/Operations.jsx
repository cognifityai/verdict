import React, { useEffect, useState } from "react";
import {
  Line, LineChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import {
  Activity, AlertTriangle, CheckCircle2, Clock, Database, DollarSign,
  Gauge, RefreshCw, Terminal,
} from "lucide-react";

import { normalizeOperationsPayload } from "./operations-data.js";

const COLOR = {
  panel: "#111615", panel2: "#151b19", border: "#2b3532", grid: "#202925",
  text: "#edf4f1", sub: "#9aaba4", faint: "#68766f", accent: "#4ee1aa",
  blue: "#56b6ff", green: "#56d39b", red: "#ff746a", amber: "#efbd63",
  redBg: "#2d1918", amberBg: "#2a2418", greenBg: "#172a22",
};

export function useOperations(url) {
  const [state, setState] = useState({ data: null, loading: true, error: null, running: null });
  const requestId = React.useRef(0);

  const load = React.useCallback(async () => {
    const activeRequest = ++requestId.current;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = normalizeOperationsPayload(await response.json());
      if (activeRequest !== requestId.current) return;
      setState((current) => ({ ...current, data, loading: false, error: null }));
    } catch (_error) {
      if (activeRequest !== requestId.current) return;
      setState((current) => ({ ...current, loading: false, error: "Operations data is temporarily unavailable." }));
    }
  }, [url]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60_000);
    return () => {
      clearInterval(timer);
      requestId.current += 1;
    };
  }, [load]);

  const run = React.useCallback(async (actionId) => {
    if (!state.data?.csrfToken || state.running) return;
    setState((current) => ({ ...current, running: actionId, error: null }));
    try {
      const response = await fetch(`${url}/jobs`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": state.data.csrfToken,
        },
        body: JSON.stringify({ kind: actionId }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await response.json();
      setState((current) => ({ ...current, running: null }));
      await load();
    } catch (_error) {
      setState((current) => ({ ...current, running: null, error: "The backend job did not complete successfully." }));
    }
  }, [load, state.data, state.running, url]);

  return { ...state, load, run };
}

function formatValue(value, unit) {
  if (value == null) return "Unavailable";
  if (unit === "percent") return `${(value * 100).toFixed(1)}%`;
  if (unit === "ms") return `${value.toFixed(value < 10 ? 2 : 0)} ms`;
  if (unit === "usd") return `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`;
  if (unit === "rpm") return `${value.toFixed(1)} rpm`;
  return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
}

function statusColor(status) {
  return status === "available" ? COLOR.green : status === "partial" ? COLOR.amber : COLOR.faint;
}

function Panel({ children, className = "" }) {
  return <section className={`border ${className}`} style={{ borderColor: COLOR.border, background: COLOR.panel, borderRadius: 3 }}>{children}</section>;
}

function MetricCard({ metric }) {
  return (
    <Panel className="p-4 min-w-0">
      <div className="flex items-start justify-between gap-2">
        <div className="text-xs font-mono" style={{ color: COLOR.sub }}>{metric.label}</div>
        <span className="w-2 h-2 mt-1 shrink-0" style={{ background: statusColor(metric.status), borderRadius: 9 }} />
      </div>
      <div className="font-semibold mt-2" style={{ color: metric.value == null ? COLOR.faint : COLOR.text, fontSize: 22 }}>
        {formatValue(metric.value, metric.unit)}
      </div>
      {metric.message && <div className="text-xs mt-1" style={{ color: COLOR.faint }}>{metric.message}</div>}
      {metric.series.length > 1 && (
        <div className="mt-3" style={{ height: 92 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={metric.series} margin={{ top: 4, right: 3, left: -28, bottom: 0 }}>
              <CartesianGrid stroke={COLOR.grid} vertical={false} />
              <XAxis dataKey="timestamp" hide />
              <YAxis tick={{ fill: COLOR.faint, fontSize: 9 }} stroke={COLOR.border} width={42} />
              <Tooltip contentStyle={{ background: COLOR.panel2, border: `1px solid ${COLOR.border}`, fontSize: 11 }} formatter={(value) => formatValue(value, metric.unit)} labelFormatter={() => ""} />
              <Line type="monotone" dataKey="value" stroke={COLOR.blue} strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Panel>
  );
}

function CostBreakdown({ breakdown }) {
  const rows = [
    ["Agent cost", breakdown?.agent],
    ["Verdict / judge cost", breakdown?.judge],
    ["Unclassified cost", breakdown?.unclassified],
  ];
  return (
    <Panel className="p-4">
      <div className="flex items-center gap-2 font-semibold text-sm"><DollarSign size={15} style={{ color: COLOR.accent }} />Cost provenance</div>
      <div className="grid sm:grid-cols-3 gap-3 mt-3">
        {rows.map(([label, item]) => (
          <div key={label} className="p-3" style={{ background: COLOR.panel2 }}>
            <div className="text-xs" style={{ color: COLOR.sub }}>{label}</div>
            <div className="font-semibold mt-1" style={{ fontSize: 18 }}>{formatValue(item?.cost ?? null, "usd")}</div>
            <div className="text-xs mt-1" style={{ color: COLOR.faint }}>{item?.traces ?? 0} attributed traces · {item?.status || "unavailable"}</div>
          </div>
        ))}
      </div>
      <div className="text-xs mt-3" style={{ color: COLOR.faint }}>Costs come from captured provider traces. Unknown/custom workloads remain unclassified rather than being guessed.</div>
    </Panel>
  );
}

function Jobs({ jobs }) {
  return (
    <Panel className="overflow-hidden">
      <div className="px-4 py-3 border-b flex items-center gap-2" style={{ borderColor: COLOR.border }}><Terminal size={15} style={{ color: COLOR.blue }} /><span className="font-semibold text-sm">Recent backend jobs</span></div>
      {jobs.length === 0 ? <div className="p-5 text-sm" style={{ color: COLOR.faint }}>No backend jobs have run yet.</div> : (
        <div className="divide-y" style={{ borderColor: COLOR.border }}>
          {jobs.map((job) => (
            <details key={job.id} className="px-4 py-3" style={{ borderColor: COLOR.border }}>
              <summary className="cursor-pointer flex flex-wrap items-center gap-2 text-sm">
                {job.status === "succeeded" ? <CheckCircle2 size={14} style={{ color: COLOR.green }} /> : job.status === "failed" ? <AlertTriangle size={14} style={{ color: COLOR.red }} /> : <Clock size={14} style={{ color: COLOR.amber }} />}
                <span className="font-medium">{job.kind.replaceAll("_", " ")}</span>
                <span className="text-xs" style={{ color: COLOR.faint }}>{job.startedAt}</span>
                <span className="ml-auto text-xs" style={{ color: job.status === "succeeded" ? COLOR.green : job.status === "failed" ? COLOR.red : COLOR.amber }}>{job.status}</span>
              </summary>
              {job.summary && <div className="text-xs mt-2" style={{ color: COLOR.sub }}>{job.summary}</div>}
              {job.logs.length > 0 && <pre className="text-xs mt-2 p-3 overflow-x-auto" style={{ color: COLOR.sub, background: COLOR.panel2, whiteSpace: "pre-wrap" }}>{job.logs.join("\n")}</pre>}
            </details>
          ))}
        </div>
      )}
    </Panel>
  );
}

export function Operations({ url, costBreakdown }) {
  const state = useOperations(url);
  const data = state.data;
  if (!data && state.loading) return <div className="p-8 text-sm" style={{ color: COLOR.sub }}>Loading operations telemetry…</div>;

  const groups = data ? [...new Set(data.metrics.map((metric) => metric.group))] : [];
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm" style={{ color: COLOR.sub }}>Application, infrastructure, database, and Verdict overhead in one admin view.</div>
          {data?.generatedAt && <div className="text-xs mt-1" style={{ color: COLOR.faint }}>Updated {data.generatedAt} · {data.windowMinutes || "bounded"}-minute window</div>}
        </div>
        <button onClick={state.load} disabled={state.loading} className="px-3 py-2 border text-xs flex items-center gap-2" style={{ borderColor: COLOR.border, color: COLOR.sub }}><RefreshCw size={14} />Refresh</button>
      </div>
      {state.error && <div role="alert" className="p-3 border text-sm flex gap-2" style={{ borderColor: COLOR.red, background: COLOR.redBg }}><AlertTriangle size={16} style={{ color: COLOR.red }} />{state.error}</div>}
      {data && groups.map((group) => (
        <section key={group}>
          <div className="text-xs font-mono uppercase mb-2 flex items-center gap-2" style={{ color: COLOR.accent }}>{group === "database" ? <Database size={14} /> : group === "application" ? <Activity size={14} /> : <Gauge size={14} />}{group}</div>
          <div className="grid sm:grid-cols-2 xl:grid-cols-4 gap-3">{data.metrics.filter((metric) => metric.group === group).map((metric) => <MetricCard key={metric.id} metric={metric} />)}</div>
        </section>
      ))}
      <CostBreakdown breakdown={costBreakdown} />
      {data && (
        <div className="grid xl:grid-cols-2 gap-5">
          <Panel className="p-4">
            <div className="font-semibold text-sm">Run Verdict jobs</div>
            <div className="space-y-3 mt-3">{data.actions.map((action) => (
              <div key={action.id} className="p-3 flex items-start gap-3" style={{ background: COLOR.panel2 }}>
                <div className="min-w-0 flex-1"><div className="text-sm font-medium">{action.label}</div><div className="text-xs mt-1" style={{ color: COLOR.faint }}>{action.enabled ? action.description : action.disabledReason}</div></div>
                <button onClick={() => state.run(action.id)} disabled={!action.enabled || Boolean(state.running)} className="px-3 py-2 border text-xs shrink-0" style={{ borderColor: action.enabled ? COLOR.accent : COLOR.border, color: action.enabled ? COLOR.accent : COLOR.faint, opacity: state.running ? 0.6 : 1 }}>{state.running === action.id ? "Running…" : "Run"}</button>
              </div>
            ))}</div>
          </Panel>
          <Panel className="p-4">
            <div className="font-semibold text-sm">Cloud consoles</div>
            <div className="space-y-2 mt-3">{data.links.map((link) => <a key={link.url} href={link.url} target="_blank" rel="noreferrer" className="block p-3 text-sm" style={{ color: COLOR.blue, background: COLOR.panel2 }}>{link.label} ↗</a>)}</div>
          </Panel>
        </div>
      )}
      {data && <Jobs jobs={data.jobs} />}
    </div>
  );
}
