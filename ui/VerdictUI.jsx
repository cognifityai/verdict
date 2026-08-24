import React, { useState, useEffect, useRef } from "react";
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, ReferenceArea, Cell,
} from "recharts";
import {
  Activity, AlertTriangle, ArrowRight, ArrowLeft, BarChart3, Boxes, CheckCircle2,
  Clock, Code2, Database, GitBranch, Layers, Scale, Search, Shield, Signal,
  TrendingDown, TrendingUp, Zap, Github, Terminal, Gauge, FlaskConical, Cpu, DollarSign,
  Filter, X, Sparkles, ChevronRight, Eye, Network, RefreshCw,
} from "lucide-react";
import { dimensionAxisLabel, dimensionLabel } from "./dimension-labels.js";
import { providerPresentation } from "./provider-presentation.js";
import { Operations } from "./Operations.jsx";
import { Registry } from "./Registry.jsx";

// Embedded synthetic sample data. This keeps the static dashboard renderable when
// no live API is reachable. It is not a benchmark, experiment result, or claim
// about any provider.
const SEED = (() => {
  const tsRows = [
    { hour: 0, anthropic_lat: 2.1, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.4, openai_err: 0, openai_n: 12, google_lat: 2.8, google_err: 0, google_n: 12 },
    { hour: 1, anthropic_lat: 2.0, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.5, openai_err: 0, openai_n: 12, google_lat: 2.9, google_err: 0, google_n: 12 },
    { hour: 2, anthropic_lat: 2.2, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.3, openai_err: 0, openai_n: 12, google_lat: 3.0, google_err: 0, google_n: 12 },
    { hour: 3, anthropic_lat: 2.1, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.6, openai_err: 0, openai_n: 12, google_lat: 2.7, google_err: 0, google_n: 12 },
    { hour: 4, anthropic_lat: 2.4, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.4, openai_err: 0, openai_n: 12, google_lat: 3.2, google_err: 0, google_n: 12 },
    { hour: 5, anthropic_lat: 2.5, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.5, openai_err: 0, openai_n: 12, google_lat: 3.0, google_err: 0, google_n: 12 },
    { hour: 6, anthropic_lat: 2.4, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.6, openai_err: 0, openai_n: 12, google_lat: 3.1, google_err: 0, google_n: 12 },
    { hour: 7, anthropic_lat: 2.3, anthropic_err: 0, anthropic_n: 12, openai_lat: 2.5, openai_err: 0, openai_n: 12, google_lat: 3.0, google_err: 0, google_n: 12 },
  ];
  return {
    meta: {
      runStart: 'sample-data', durationHours: 8, totalTraces: 96, totalJudged: 48,
      totalCost: 0.42, totalCostStatus: 'complete', regressionHour: 4,
      providers: 3, clusters: 4, workload: 'sample-service',
    },
    driftAnalysis: {
      runStatus: 'completed_with_signals', readinessStatus: 'global_minimum_met',
      current: 48, baseline: 48, minimum: 30,
      currentHours: 24, baselineLagHours: 24, baselineDays: 7,
    },
    evaluation: {
      status: "selected", selectedId: "sample-evaluator",
      selectedIdentity: { id: "sample-evaluator", label: "sample-judge · sample-rubric v1", complete: true },
      availableIdentities: [{ id: "sample-evaluator", label: "sample-judge · sample-rubric v1", complete: true }],
      driftStatus: "selected", unattributedDriftSignals: 0,
    },
    scoreCoverage: { pass: 223, fail: 17, unclear: 0, missing: 0, error: 0, evaluable: 240 },
    evaluatorHealth: [{
      id: "sample-health", evaluatedAt: "sample-data", sentinelSetName: "sample-anchor-v1",
      correctExamples: 27, totalExamples: 30, exampleAgreement: 90,
      exampleConfidenceLow: 74.4, exampleConfidenceHigh: 96.5,
      correctLabels: 135, totalLabels: 150, labelAgreement: 90,
      status: "healthy", errorCount: 0, methodVersion: "2",
    }],
    providers: [
      { key: 'anthropic', label: 'Anthropic sample', model: 'sample-model-a', n: 32, errors: 0, errorRate: 0, avgLatency: 2.25, inTok: 2400, outTok: 9600, cost: 0.19, passRate: 82, judged: 16 },
      { key: 'openai', label: 'OpenAI sample', model: 'sample-model-b', n: 32, errors: 0, errorRate: 0, avgLatency: 2.48, inTok: 2380, outTok: 8200, cost: 0.08, passRate: 96, judged: 16 },
      { key: 'google', label: 'Google sample', model: 'sample-model-c', n: 32, errors: 1, errorRate: 3.1, avgLatency: 2.96, inTok: 2450, outTok: 10400, cost: 0.15, passRate: 94, judged: 16 },
    ],
    clusters: [
      { cluster_id: 'support', n: 28 },
      { cluster_id: 'coding', n: 26 },
      { cluster_id: 'writing', n: 22 },
      { cluster_id: 'analysis', n: 20 },
    ],
    clusterHealth: {
      status: 'ready', nTraces: 96, nClusters: 4, medianClusterSize: 24,
      clustersMeetingSampleFloor: 4, minSampleSize: 12, messages: [],
    },
    driftSignals: [
      { id: 'sample-01', clusterId: 'support', dimension: 'completeness', direction: 'regression', provider: 'anthropic', providerLabel: 'Anthropic sample', statName: 'fisher_exact', stat: 7.2, p: 0.0009, pAdj: 0.0042, cliffsDelta: -0.42, cohensD: -1.1, nCur: 12, nBase: 12, layers: ['judge_rubric'], exampleTraceIds: ['sample-trace-001'], action: 'Review affected traces and compare the current prompt/model configuration against the reference window.', detectedAt: 'sample-data' },
      { id: 'sample-02', clusterId: 'support', dimension: 'instruction_following', direction: 'regression', provider: 'anthropic', providerLabel: 'Anthropic sample', statName: 'fisher_exact', stat: 5.8, p: 0.0021, pAdj: 0.0095, cliffsDelta: -0.35, cohensD: -0.9, nCur: 12, nBase: 12, layers: ['judge_rubric'], exampleTraceIds: ['sample-trace-001'], action: 'Inspect sampled failures and confirm whether the change is meaningful for this workload.', detectedAt: 'sample-data' },
    ],
    driftRun: {
      id: "sample-run", analysisTime: "sample-data", completedAt: "sample-data",
      signalCount: 2,
    },
    dimensionOverall: [
      { dim: 'groundedness', passRate: 96, pass: 46, fail: 2, unclear: 0, tot: 48 },
      { dim: 'relevance', passRate: 94, pass: 45, fail: 3, unclear: 0, tot: 48 },
      { dim: 'completeness', passRate: 88, pass: 42, fail: 6, unclear: 0, tot: 48 },
      { dim: 'safety', passRate: 98, pass: 47, fail: 1, unclear: 0, tot: 48 },
      { dim: 'instruction_following', passRate: 90, pass: 43, fail: 5, unclear: 0, tot: 48 },
    ],
    tsRows,
    passrate: [
      { hour: 0, anthropic: 98, openai: 96, google: 95 },
      { hour: 1, anthropic: 98, openai: 96, google: 95 },
      { hour: 2, anthropic: 96, openai: 96, google: 94 },
      { hour: 3, anthropic: 96, openai: 95, google: 95 },
      { hour: 4, anthropic: 88, openai: 96, google: 94 },
      { hour: 5, anthropic: 78, openai: 97, google: 94 },
      { hour: 6, anthropic: 74, openai: 96, google: 93 },
      { hour: 7, anthropic: 82, openai: 96, google: 94 },
    ],
    clusterPassrate: [],
    haikuDim: [
      { hour: 0, groundedness: 98, relevance: 98, completeness: 98, safety: 100, instruction_following: 98 },
      { hour: 1, groundedness: 98, relevance: 98, completeness: 96, safety: 100, instruction_following: 98 },
      { hour: 2, groundedness: 98, relevance: 96, completeness: 96, safety: 98, instruction_following: 96 },
      { hour: 3, groundedness: 98, relevance: 96, completeness: 96, safety: 98, instruction_following: 96 },
      { hour: 4, groundedness: 96, relevance: 92, completeness: 84, safety: 98, instruction_following: 86 },
      { hour: 5, groundedness: 96, relevance: 90, completeness: 72, safety: 96, instruction_following: 78 },
      { hour: 6, groundedness: 94, relevance: 88, completeness: 68, safety: 96, instruction_following: 74 },
      { hour: 7, groundedness: 96, relevance: 90, completeness: 76, safety: 98, instruction_following: 80 },
    ],
    samples: [
      { trace_id: 'sample-trace-001', provider: 'anthropic', request_model: 'sample-model-a', cluster_id: 'support', input_tokens: 42, output_tokens: 180, latency_ms: 2150, cost_usd: 0.003, finish_reason: 'end_turn', error: null, started_at: 'sample-data', prompt_redacted: 'Summarize the refund policy for a delayed shipment.', response_redacted: 'Sample response text shown for layout only. Connect the dashboard to a real Verdict database to inspect captured responses.', hour: 4.1, judgment: { judges: ['sample-judge'], dims: [
        { name: 'groundedness', verdict: 'pass', reasoning: 'The sample response stays within the supplied context.' },
        { name: 'relevance', verdict: 'pass', reasoning: 'The response addresses the support request.' },
        { name: 'completeness', verdict: 'fail', reasoning: 'The response omits an important policy condition.' },
        { name: 'safety', verdict: 'pass', reasoning: 'No unsafe content is present.' },
        { name: 'instruction_following', verdict: 'fail', reasoning: 'The response did not follow the requested format.' },
      ] } },
      { trace_id: 'sample-trace-002', provider: 'openai', request_model: 'sample-model-b', cluster_id: 'coding', input_tokens: 35, output_tokens: 150, latency_ms: 2400, cost_usd: 0.001, finish_reason: 'end_turn', error: null, started_at: 'sample-data', prompt_redacted: 'Write a small retry helper with exponential backoff.', response_redacted: 'Sample code response placeholder.', hour: 4.2 },
      { trace_id: 'sample-trace-003', provider: 'google', request_model: 'sample-model-c', cluster_id: 'writing', input_tokens: 28, output_tokens: 140, latency_ms: 3100, cost_usd: 0.002, finish_reason: 'end_turn', error: null, started_at: 'sample-data', prompt_redacted: 'Draft a concise release note for a dashboard update.', response_redacted: 'Sample writing response placeholder.', hour: 4.3 },
      { trace_id: 'sample-trace-004', provider: 'anthropic', request_model: 'sample-model-a', cluster_id: 'analysis', input_tokens: 52, output_tokens: 170, latency_ms: 2250, cost_usd: 0.003, finish_reason: 'end_turn', error: null, started_at: 'sample-data', prompt_redacted: 'Compare two support-routing policies.', response_redacted: 'Sample analytical response placeholder.', hour: 5.1 },
      { trace_id: 'sample-trace-005', provider: 'openai', request_model: 'sample-model-b', cluster_id: 'support', input_tokens: 30, output_tokens: 110, latency_ms: 2500, cost_usd: 0.001, finish_reason: 'end_turn', error: null, started_at: 'sample-data', prompt_redacted: 'Explain why an invoice total changed.', response_redacted: 'Sample support response placeholder.', hour: 5.2 },
      { trace_id: 'sample-trace-006', provider: 'google', request_model: 'sample-model-c', cluster_id: 'coding', input_tokens: 46, output_tokens: 210, latency_ms: 3200, cost_usd: 0.002, finish_reason: 'end_turn', error: null, started_at: 'sample-data', prompt_redacted: 'Review a short SQL migration for safety.', response_redacted: 'Sample review response placeholder.', hour: 5.3 },
    ],
    providerDimension: [
      { dim: 'groundedness', anthropic: 94, openai: 96, google: 96 },
      { dim: 'relevance', anthropic: 88, openai: 96, google: 94 },
      { dim: 'completeness', anthropic: 74, openai: 94, google: 92 },
      { dim: 'safety', anthropic: 96, openai: 100, google: 98 },
      { dim: 'instruction_following', anthropic: 78, openai: 94, google: 92 },
    ],
    truncation: { applied: false, resources: {} },
  };
})();

const EMPTY = {
  meta: { totalTraces: 0, totalJudged: 0, totalCost: null, providers: 0, clusters: 0, workload: null },
  driftAnalysis: {
    runStatus: 'no_completed_run', readinessStatus: 'not_enough_current',
    current: 0, baseline: 0, minimum: 30,
    currentHours: 24, baselineLagHours: 24, baselineDays: 7,
  },
  evaluation: { status: "empty", selectedId: null, availableIdentities: [], driftStatus: "empty", unattributedDriftSignals: 0 },
  providers: [], clusters: [], driftSignals: [], dimensionOverall: [], tsRows: [],
  passrate: [], clusterPassrate: [], haikuDim: [], samples: [], providerDimension: [],
  evaluatorHealth: [], scoreCoverage: {}, truncation: { applied: false, resources: {} },
};

function mountedApiUrl() {
  if (typeof window === "undefined") return "/api/data";
  if (window.VERDICT_API) return window.VERDICT_API;
  const base = window.location.pathname.replace(/\/dashboard\/?$/, "").replace(/\/$/, "");
  return `${base}/api/data`;
}

function mountedConfigUrl() {
  return mountedApiUrl().replace(/\/api\/data(?:\?.*)?$/, "/api/config");
}

function mountedRegistryUrl() {
  return mountedApiUrl().replace(/\/api\/data(?:\?.*)?$/, "/api/registry");
}

function useOperationsConfig() {
  const [operationsUrl, setOperationsUrl] = useState(null);
  useEffect(() => {
    let active = true;
    fetch(mountedConfigUrl(), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : null)
      .then((config) => {
        const value = config?.operationsUrl;
        if (active && typeof value === "string" && value.startsWith("/") && !value.startsWith("//")) setOperationsUrl(value);
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);
  return operationsUrl;
}

const API_URL = mountedApiUrl();

function apiUrlForEvaluator(evaluatorId) {
  if (!evaluatorId) return API_URL;
  const separator = API_URL.includes("?") ? "&" : "?";
  return `${API_URL}${separator}evaluator=${encodeURIComponent(evaluatorId)}`;
}

function useDashboardData() {
  const [state, setState] = useState({
    snapshot: EMPTY,
    source: "loading",
    loading: false,
    error: null,
  });
  const requestSequence = useRef(0);
  const activeController = useRef(null);

  const load = React.useCallback(async (evaluatorId = null) => {
    const requestId = requestSequence.current + 1;
    requestSequence.current = requestId;
    if (activeController.current) activeController.current.abort();
    const controller = new AbortController();
    activeController.current = controller;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(apiUrlForEvaluator(evaluatorId), {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const snapshot = await response.json();
      if (!snapshot || !snapshot.meta || !Array.isArray(snapshot.providers)) {
        throw new Error("invalid dashboard response");
      }
      if (requestId !== requestSequence.current) return false;
      setState({ snapshot, source: "live", loading: false, error: null });
      return true;
    } catch (error) {
      if (requestId !== requestSequence.current) return false;
      if (error?.name === "AbortError") return false;
      setState((current) => {
        const confirmed = current.snapshot.evaluation?.selectedId;
        const requested = evaluatorId || "the default evaluator";
        const shown = confirmed || "the current snapshot";
        return {
          ...current,
          loading: false,
          error: `Could not load ${requested}. Still showing ${shown}.`,
        };
      });
      return false;
    } finally {
      if (requestId === requestSequence.current) activeController.current = null;
    }
  }, []);

  useEffect(() => {
    load();
    return () => {
      requestSequence.current += 1;
      if (activeController.current) activeController.current.abort();
    };
  }, [load]);

  return {
    ...state,
    load,
    reload: () => load(state.snapshot.evaluation?.selectedId || null),
  };
}

/* ---------------------------------------------------------------- palette */
const C = {
  bg: "#0b0e0d", panel: "#111615", panel2: "#151b19", raised: "#1b2421",
  border: "#2b3532", grid: "#202925", text: "#edf4f1", sub: "#9aaba4",
  faint: "#68766f", accent: "#4ee1aa", accent2: "#56b6ff", navy: "#0b0e0d",
  cyan: "#55d9e8", green: "#56d39b", red: "#ff746a", amber: "#efbd63", blue: "#82aaff",
  redBg: "#2d1918", amberBg: "#2a2418", greenBg: "#172a22", blueBg: "#17242c",
};
/* ---------------------------------------------------------------- helpers */
const pct = (n) => (n == null ? "—" : `${n}%`);
const usd = (n) => (n == null ? "Unavailable" : `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`);
const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`);
const shortTraceId = (id) => id.length > 20 ? `${id.slice(0, 8)}…${id.slice(-8)}` : id;
const sci = (n) => {
  if (n == null || !Number.isFinite(n)) return "n/a";
  if (n === 0) return "0";
  if (n >= 0.001) return n.toFixed(4);
  const e = Math.floor(Math.log10(n));
  return `${(n / Math.pow(10, e)).toFixed(1)}e${e}`;
};
const coveragePct = (n) => n == null || !Number.isFinite(n) ? "n/a" : `${Math.round(n * 1000) / 10}%`;
const statValue = (n) => n == null || !Number.isFinite(n) ? "n/a" : n;
const TRACE_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function capturedContent(sample) {
  if (typeof sample.contentCaptured === "boolean") return sample.contentCaptured;
  return sample.prompt_redacted != null || sample.response_redacted != null;
}

function traceTime(sample) {
  const date = new Date(sample.started_at);
  if (!Number.isFinite(date.getTime())) {
    return {
      absolute: Number.isFinite(sample.hour) ? `Elapsed ${sample.hour}h` : "Recorded time unavailable",
      relative: "",
    };
  }
  const absolute = `${TRACE_MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}, ${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")} UTC`;
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  let relative = "just now";
  if (elapsedSeconds >= 86400) relative = `${Math.floor(elapsedSeconds / 86400)} days ago`;
  else if (elapsedSeconds >= 3600) relative = `${Math.floor(elapsedSeconds / 3600)} hours ago`;
  else if (elapsedSeconds >= 60) relative = `${Math.floor(elapsedSeconds / 60)} minutes ago`;
  return { absolute, relative };
}

function driftAnalysis(data) {
  return data.driftAnalysis || {
    runStatus: data.driftRun ? (data.driftSignals.length ? "completed_with_signals" : "completed_no_signals") : "no_completed_run",
    readinessStatus: "not_enough_current",
    current: 0,
    baseline: 0,
    minimum: 30,
    currentHours: 24,
    baselineLagHours: 24,
    baselineDays: 7,
  };
}

function analysisHeadline(analysis) {
  if (analysis.runStatus === "selection_required") return "Select an evaluator";
  if (analysis.runStatus === "invalid_selection") return "Selected evaluator is unavailable";
  if (analysis.runStatus === "completed_with_signals") return "Completed with signals";
  if (analysis.runStatus === "completed_no_signals") return "Completed with no signals";
  if (analysis.readinessStatus === "not_enough_baseline") return "Collecting baseline traces";
  if (analysis.readinessStatus === "global_minimum_met") return "Global trace minimum met";
  return "Collecting current traces";
}

function ReadinessSummary({ analysis }) {
  const globalMinimumMet = analysis.readinessStatus === "global_minimum_met";
  return (
    <div className="mt-4">
      <div className="grid sm:grid-cols-2 gap-3">
        <div className="rounded-md p-3" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
          <div className="text-xs" style={{ color: C.sub }}>Current global content-bearing traces</div>
          <div className="font-semibold mt-0.5" style={{ fontSize: 17 }}>{`${analysis.current} / ${analysis.minimum}`}</div>
        </div>
        <div className="rounded-md p-3" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
          <div className="text-xs" style={{ color: C.sub }}>Baseline global content-bearing traces</div>
          <div className="font-semibold mt-0.5" style={{ fontSize: 17 }}>{`${analysis.baseline} / ${analysis.minimum}`}</div>
        </div>
      </div>
      <div className="grid sm:grid-cols-3 gap-3 mt-3">
        {[
          ["Default current window", `Latest ${analysis.currentHours} hours`],
          ["Default baseline lag", `${analysis.baselineLagHours} hours`],
          ["Default baseline window", `Previous ${analysis.baselineDays} days`],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md p-3" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
            <div className="text-xs" style={{ color: C.sub }}>{label}</div>
            <div className="font-semibold mt-0.5" style={{ fontSize: 15 }}>{value}</div>
          </div>
        ))}
      </div>
      <div className="text-xs mt-3" style={{ color: C.faint }}>
        {globalMinimumMet
          ? "Enough content-bearing traces exist across the overall default windows. The pipeline will still check whether each cluster and rubric dimension has enough judged traces."
          : "These global counts show content availability only. They do not establish whether each cluster and rubric dimension has enough judged traces."}
        {" "}Actual job flags may use different windows or sample floors.
      </div>
      <div className="text-xs mt-2" style={{ color: C.faint }}>
        New traces cannot simultaneously be recent current data and historical baseline data. The baseline matures only after its {analysis.baselineLagHours}-hour lag.
      </div>
    </div>
  );
}

function ChartTooltip({ active, payload, label, unit = "", title }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div style={{ background: C.panel2, border: `1px solid ${C.border}`, borderRadius: 3, padding: "8px 11px", boxShadow: "0 12px 32px rgba(0,0,0,.32)" }}>
      <div style={{ color: C.sub, fontSize: 11, marginBottom: 4 }}>{title ? title : `Hour ${label}`}</div>
      {payload.map((p, i) => (
        <div key={i} className="flex items-center gap-2" style={{ fontSize: 12.5 }}>
          <span style={{ width: 8, height: 8, borderRadius: 2, background: p.color || p.fill }} />
          <span style={{ color: C.sub }}>{p.name}</span>
          <span style={{ color: C.text, fontWeight: 600, marginLeft: "auto" }}>
            {p.value == null ? "—" : `${p.value}${unit}`}
          </span>
        </div>
      ))}
    </div>
  );
}

function Panel({ children, className = "", style = {} }) {
  return (
    <div className={`border ${className}`}
      style={{ background: C.panel, borderColor: C.border, ...style }}>
      {children}
    </div>
  );
}

function Pill({ children, color = C.sub, bg }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5"
      style={{ color, background: bg || "transparent", border: `1px solid ${bg || C.border}`, borderRadius: 3 }}>
      {children}
    </span>
  );
}

function Dot({ color }) {
  return <span style={{ width: 8, height: 8, borderRadius: 99, background: color, display: "inline-block" }} />;
}

function Logo({ size = 26 }) {
  return (
    <div className="flex items-center gap-2">
      <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
        <rect width="32" height="32" rx="3" fill={C.panel2} stroke={C.border} />
        <path d="M5 5 H27" stroke={C.cyan} strokeWidth="2" />
        <path d="M9 10.5 L16 22 L23 10.5" stroke={C.text} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="16" cy="22.5" r="1.7" fill={C.text} />
      </svg>
    </div>
  );
}

/* ================================================================ LANDING */
function Landing({ onEnter }) {
  const problems = [
    { icon: TrendingDown, title: "Model behavior changes", body: "Provider, model, or prompt changes can shift response quality. Verdict helps surface those changes by workload." },
    { icon: DollarSign, title: "Cost changes need context", body: "Prompt and model changes can alter token usage across captured calls. Verdict tracks cost and latency alongside quality." },
    { icon: Network, title: "Model comparisons take work", body: "Comparing providers on your own traffic usually requires custom logging, labeling, and analysis." },
  ];
  const steps = [
    { icon: Activity, t: "Capture", d: "One line of init instruments supported provider SDK calls into Verdict's vendor-neutral Trace schema." },
    { icon: Database, t: "Store", d: "The SDK stores traces through one protocol: SQLite locally, Postgres for shared environments, or memory for tests. The bundled dashboard reads SQLite and Postgres." },
    { icon: Layers, t: "Cluster", d: "Persistent MiniLM-based assignment groups similar prompts so baseline and current windows compare like with like." },
    { icon: Scale, t: "Judge", d: "A configurable judge model scores sampled responses on 5 binary dimensions, reasoning before its verdict." },
    { icon: Signal, t: "Detect drift", d: "Fisher's exact test (binary dimensions) / Mann-Whitney U + Benjamini-Hochberg per (cluster, dimension), gated on Cliff's δ." },
    { icon: GitBranch, t: "Compare", d: "Bradley-Terry pairwise ranking across providers on your own traffic, with bootstrap CIs." },
  ];
  const validation = [
    { k: "Capture", l: "Provider call tracing", d: "Instrument supported SDK calls and verify live capture with the release-check script.", icon: FlaskConical, color: C.green },
    { k: "Judge", l: "Rubric calibration", d: "Use your own labeled traces to measure judge agreement before relying on alerts.", icon: Scale, color: C.blue },
    { k: "Drift", l: "Statistical review", d: "Inspect changes by cluster and dimension instead of relying on one global average.", icon: Signal, color: C.accent2 },
  ];
  const limits = [
    "Agent-run and tool-call graphs are planned work",
    "Judge calibration is workload-specific",
    "Redaction is best-effort pattern matching",
    "Dashboard is intended for local or trusted-network use",
  ];
  return (
    <div style={{ background: C.bg, color: C.text, minHeight: "100%" }}>
      {/* nav */}
      <div className="sticky top-0 z-20 border-b" style={{ borderColor: C.border, background: "rgba(11,14,13,0.95)" }}>
        <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <Logo />
            <span className="font-semibold" style={{ fontSize: 16, letterSpacing: 0 }}>Verdict</span>
            <Pill color={C.sub}>v0 · open source</Pill>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden sm:inline-flex items-center gap-1.5 text-sm px-3 py-1.5" style={{ color: C.sub }}>
              <Github size={15} /> Apache 2.0
            </span>
            <button onClick={onEnter} className="inline-flex items-center gap-1.5 text-sm font-medium px-3.5 py-1.5"
              style={{ background: C.accent, color: C.bg, borderRadius: 3 }}>
              Open dashboard <ArrowRight size={15} />
            </button>
          </div>
        </div>
      </div>

      {/* hero */}
      <div className="max-w-6xl mx-auto px-6 pt-16 pb-14">
        <div className="max-w-3xl">
          <Pill color={C.cyan} bg={C.blueBg}><Sparkles size={12} /> LLM-call observability · drift detection</Pill>
          <h1 className="mt-5 font-semibold" style={{ fontSize: 46, lineHeight: 1.07, letterSpacing: 0 }}>
            Monitor LLM-call quality on your <span style={{ color: C.accent2 }}>own workloads.</span>
          </h1>
          <p className="mt-5 text-lg" style={{ color: C.sub, lineHeight: 1.6 }}>
            Verdict instruments supported model calls with one line, evaluates sampled outputs against your configured rubric,
            and helps you inspect quality, cost, and behavior changes on
            <span style={{ color: C.text }}> your own traffic</span> instead of relying on generic benchmarks.
          </p>
          <div className="mt-7 flex flex-wrap items-center gap-3">
            <button onClick={onEnter} className="inline-flex items-center gap-2 text-sm font-semibold px-5 py-2.5 rounded-md"
              style={{ background: C.accent, color: C.bg }}>
              Explore the dashboard <ArrowRight size={16} />
            </button>
            <a href="https://github.com/cognifityai/verdict" target="_blank" rel="noreferrer"
              className="inline-flex items-center gap-2 text-sm px-4 py-2.5 rounded-md border" style={{ borderColor: C.border, color: C.sub }}>
              <Github size={16} /> Read the source
            </a>
          </div>
          <div className="mt-7 flex flex-wrap gap-x-6 gap-y-2 text-sm" style={{ color: C.faint }}>
            <span className="flex items-center gap-1.5"><CheckCircle2 size={14} style={{ color: C.green }} /> Synthetic sample data included</span>
            <span className="flex items-center gap-1.5"><CheckCircle2 size={14} style={{ color: C.green }} /> 3 providers, 4 intent clusters</span>
            <span className="flex items-center gap-1.5"><CheckCircle2 size={14} style={{ color: C.green }} /> Live API replaces the sample when connected</span>
          </div>
        </div>
      </div>

      {/* problems */}
      <Section title="Three recurring problems in production LLM systems">
        <div className="grid md:grid-cols-3 gap-4">
          {problems.map((p) => (
            <Panel key={p.title} className="p-5">
              <div className="w-9 h-9 flex items-center justify-center mb-3" style={{ background: C.redBg, borderRadius: 3 }}>
                <p.icon size={18} style={{ color: C.red }} />
              </div>
              <div className="font-semibold mb-1.5">{p.title}</div>
              <div className="text-sm" style={{ color: C.sub, lineHeight: 1.55 }}>{p.body}</div>
            </Panel>
          ))}
        </div>
      </Section>

      {/* how it works */}
      <Section title="How it works" subtitle="Capture → store → cluster → judge → detect drift → compare. Six stages, one pipeline.">
        <div className="grid md:grid-cols-3 gap-4">
          {steps.map((s, i) => (
            <Panel key={s.t} className="p-5">
              <div className="flex items-center gap-3 mb-2.5">
                <div className="w-9 h-9 rounded-md flex items-center justify-center" style={{ background: C.raised, border: `1px solid ${C.border}` }}>
                  <s.icon size={18} style={{ color: C.accent2 }} />
                </div>
                <span className="text-xs font-mono" style={{ color: C.faint }}>0{i + 1}</span>
                <span className="font-semibold">{s.t}</span>
              </div>
              <div className="text-sm" style={{ color: C.sub, lineHeight: 1.55 }}>{s.d}</div>
            </Panel>
          ))}
        </div>
      </Section>

      {/* validation */}
      <Section title="Verification paths" subtitle="The repository includes scripts for each check below.">
        <div className="grid md:grid-cols-3 gap-4">
          {validation.map((v) => (
            <Panel key={v.l} className="p-5" style={{ background: C.panel2 }}>
              <v.icon size={20} style={{ color: v.color }} />
              <div className="mt-3 font-semibold" style={{ fontSize: 26, color: v.color, letterSpacing: 0 }}>{v.k}</div>
              <div className="text-sm font-medium mt-0.5">{v.l}</div>
              <div className="text-sm mt-2" style={{ color: C.sub, lineHeight: 1.5 }}>{v.d}</div>
            </Panel>
          ))}
        </div>
      </Section>

      {/* install */}
      <Section title="The five-line install">
        <Panel className="overflow-hidden">
          <div className="flex items-center gap-2 px-4 py-2.5 border-b" style={{ borderColor: C.border, background: C.panel2 }}>
            <Terminal size={14} style={{ color: C.sub }} />
            <span className="text-xs font-mono" style={{ color: C.sub }}>app.py</span>
          </div>
          <pre className="p-5 text-sm overflow-x-auto" style={{ color: C.text, lineHeight: 1.7 }}>
<span style={{ color: C.accent2 }}>import</span> verdict{"\n"}
<span style={{ color: C.accent2 }}>from</span> anthropic <span style={{ color: C.accent2 }}>import</span> Anthropic{"\n\n"}
verdict.<span style={{ color: C.blue }}>init</span>(service_name=<span style={{ color: C.green }}>"my-app"</span>, storage=<span style={{ color: C.green }}>"sqlite:///./verdict.db"</span>){"\n"}
client = <span style={{ color: C.blue }}>Anthropic</span>(){"\n"}
<span style={{ color: C.faint }}>{"# Use Anthropic normally; supported calls are captured."}</span>
          </pre>
        </Panel>
      </Section>

      {/* honest limits */}
      <Section title="Honest limits" subtitle="What Verdict v0 does not yet claim.">
        <div className="flex flex-wrap gap-2">
          {limits.map((l) => (
            <span key={l} className="text-sm px-3 py-1.5 border" style={{ borderColor: C.border, color: C.sub, background: C.panel, borderRadius: 3 }}>{l}</span>
          ))}
        </div>
      </Section>

      {/* cta */}
      <div className="max-w-6xl mx-auto px-6 pb-20">
        <Panel className="p-8 text-center" style={{ background: C.panel2, color: C.text, borderColor: C.border }}>
          <div className="font-semibold" style={{ fontSize: 26, letterSpacing: 0 }}>Explore the sample dashboard</div>
          <div className="text-sm mt-2 max-w-xl mx-auto" style={{ color: C.sub }}>
            Walk through synthetic sample data: drift signals, trace explorer, judge scores, and provider comparison. Connect the dashboard to a Verdict database for live data.
          </div>
          <button onClick={onEnter} className="mt-5 inline-flex items-center gap-2 text-sm font-semibold px-5 py-2.5 rounded-md"
            style={{ background: C.accent, color: C.bg }}>
            Open dashboard <ArrowRight size={16} />
          </button>
        </Panel>
        <div className="text-center text-xs mt-8" style={{ color: C.faint }}>
          A Cognifity AI project · Apache 2.0 SDK · Bundled dashboard data is synthetic
        </div>
      </div>
    </div>
  );
}

function Section({ title, subtitle, children }) {
  return (
    <div className="max-w-6xl mx-auto px-6 py-9 border-t" style={{ borderColor: C.border }}>
      <h2 className="font-semibold mb-1" style={{ fontSize: 22, letterSpacing: 0 }}>{title}</h2>
      {subtitle && <p className="text-sm mb-5" style={{ color: C.sub }}>{subtitle}</p>}
      {!subtitle && <div className="mb-5" />}
      {children}
    </div>
  );
}

/* ============================================================== DASHBOARD */
const TRUNCATION_LABELS = {
  providers: "providers",
  providerModels: "provider models",
  clusters: "clusters",
  dimensions: "dimensions",
  evaluatorIdentities: "evaluator identities",
  driftSignals: "drift signals",
  latencyPoints: "latency points",
  hourlyPoints: "quality points",
  traceSamples: "trace samples",
};

function Dashboard({ data = SEED, onExit, source = "sample", onReload, onEvaluatorChange, reloading, loadError, operationsUrl = null }) {
  const DATA = data;
  const [tab, setTab] = useState("overview");
  const evaluation = DATA.evaluation || { status: "empty", selectedId: null, availableIdentities: [] };
  const evaluatorSelectionNeeded = ["selection_required", "invalid_selection"].includes(evaluation.status);
  const boundedResources = Object.entries(DATA.truncation?.resources || {})
    .filter(([, resource]) => resource.shown < resource.available);
  const sourceLabel = source === "live" ? "Live store" : source === "sample" ? "Synthetic sample" : "Waiting for live store";
  const sourceColor = source === "live" ? C.green : C.amber;
  const workloadLabel = source === "sample"
    ? "Sample workload"
    : DATA.meta.workload
      ? `Workload: ${DATA.meta.workload}`
      : "Live Verdict store";
  const nav = [
    { id: "overview", label: "Overview", icon: Gauge },
    { id: "drift", label: "Drift signals", icon: Signal, badge: DATA.driftSignals.length },
    { id: "traces", label: "Trace explorer", icon: Activity },
    { id: "registry", label: "Registry", icon: Layers },
    { id: "judge", label: "Judge scores", icon: Scale },
    { id: "compare", label: "Compare LLMs", icon: GitBranch },
    ...(operationsUrl ? [{ id: "operations", label: "Operations", icon: Cpu }] : []),
  ];
  return (
    <div style={{ background: C.bg, color: C.text, minHeight: "100%" }}>
      <header className="sticky top-0 z-20 border-b" style={{ background: "rgba(11,14,13,.96)", borderColor: C.border }}>
        <div className="max-w-screen-2xl mx-auto px-4 sm:px-6 min-h-16 flex flex-wrap items-center gap-x-5">
          <button onClick={onExit} title="Back to home" className="h-16 flex items-center gap-2 shrink-0">
            <Logo size={24} />
            <span className="font-semibold">Verdict</span>
          </button>
          <span className="hidden lg:inline text-xs font-mono" style={{ color: C.faint }}>{source === "sample" ? "Sample service" : "Live Verdict store"}</span>
          <nav className="order-3 w-full sm:order-none sm:w-auto sm:self-stretch flex overflow-x-auto border-t sm:border-t-0" style={{ borderColor: C.border }}>
            {nav.map((n) => {
              const on = tab === n.id;
              return (
                <button key={n.id} onClick={() => setTab(n.id)}
                  className="h-11 sm:h-16 shrink-0 flex items-center gap-2 px-3 text-xs sm:text-sm border-b-2"
                  style={{ color: on ? C.text : C.sub, background: on ? C.raised : "transparent", borderColor: on ? C.accent : "transparent", fontWeight: on ? 650 : 450 }}>
                  <n.icon size={15} style={{ color: on ? C.accent : C.faint }} />
                  {n.label}
                  {n.badge ? <span className="text-xs font-semibold px-1.5" style={{ background: C.redBg, color: C.red, borderRadius: 2 }}>{n.badge}</span> : null}
                </button>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-2 text-xs">
            <span className="flex items-center gap-1.5 px-1 sm:px-2 py-1" title={sourceLabel} style={{ color: sourceColor }}>
              <Dot color={sourceColor} />
              <span className="hidden sm:inline">{sourceLabel}</span>
            </span>
            <span className="hidden xl:inline font-mono" style={{ color: C.faint }}>{DATA.meta.totalTraces.toLocaleString()} traces</span>
            <button onClick={onReload} disabled={reloading} title="Refresh from API"
              className="w-8 h-8 inline-flex items-center justify-center border"
              style={{ borderColor: C.border, borderRadius: 3, color: C.sub, opacity: reloading ? 0.5 : 1 }}>
              <RefreshCw size={14} style={{ animation: reloading ? "vspin 0.8s linear infinite" : "none" }} />
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-screen-2xl mx-auto px-4 sm:px-6 py-6 overflow-x-hidden">
        <div className="mb-5 flex flex-col sm:flex-row sm:items-end justify-between gap-3">
          <div>
            <div className="text-xs font-mono" style={{ color: C.accent }}>{workloadLabel}</div>
            <h1 className="font-semibold mt-1" style={{ fontSize: 22, letterSpacing: 0 }}>{nav.find((n) => n.id === tab).label}</h1>
          </div>
          {source === "sample" && (
            <div className="max-w-full flex items-start gap-2 text-xs px-3 py-2 border" style={{ borderColor: "#6b5529", background: C.amberBg, color: C.amber, borderRadius: 3 }}>
              <FlaskConical size={14} style={{ flexShrink: 0 }} /> <span>Sample data, not a provider benchmark</span>
            </div>
          )}
        </div>
        {evaluation.availableIdentities.length > 0 && (
          <div className="mb-5 flex flex-col md:flex-row md:items-center gap-3 px-3 py-3 border"
            style={{ borderColor: evaluatorSelectionNeeded ? "#6b5529" : C.border, background: evaluatorSelectionNeeded ? C.amberBg : C.panel2, borderRadius: 3 }}>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-mono" style={{ color: evaluatorSelectionNeeded ? C.amber : C.accent2 }}>EVALUATOR IDENTITY</div>
              <div className="text-xs mt-1" style={{ color: C.sub }}>
                {evaluatorSelectionNeeded
                  ? "Select one evaluator before reading judgment summaries or trace verdicts."
                  : "All judgment summaries and trace verdicts below use only this evaluator identity."}
              </div>
            </div>
            <select aria-label="Evaluator identity" value={evaluation.selectedId || ""}
              disabled={reloading}
              onChange={(event) => onEvaluatorChange(event.target.value)}
              className="text-xs px-3 py-2 border max-w-full"
              style={{ color: C.text, background: C.panel, borderColor: C.border, borderRadius: 3 }}>
              <option value="" disabled>Select an evaluator</option>
              {evaluation.availableIdentities.map((identity) => (
                <option key={identity.id} value={identity.id}>{identity.label}</option>
              ))}
            </select>
          </div>
        )}
        {loadError && (
          <div role="alert" className="mb-5 px-3 py-3 border flex items-start gap-2 text-sm"
            style={{ borderColor: "#6b5529", background: C.amberBg, color: C.text, borderRadius: 3 }}>
            <AlertTriangle size={16} style={{ color: C.amber, marginTop: 1, flexShrink: 0 }} />
            <span>{loadError}</span>
          </div>
        )}
        {DATA.truncation?.applied && boundedResources.length > 0 && (
          <div role="status" className="mb-5 px-3 py-3 border flex items-start gap-2 text-sm"
            style={{ borderColor: "#6b5529", background: C.amberBg, color: C.text, borderRadius: 3 }}>
            <AlertTriangle size={16} style={{ color: C.amber, marginTop: 1, flexShrink: 0 }} />
            <span>
              Showing a bounded dashboard view. Store totals remain complete; presentation data is limited: {boundedResources.map(([name, resource]) => (
                `${TRUNCATION_LABELS[name] || name}: ${resource.shown.toLocaleString()} of ${resource.available.toLocaleString()}`
              )).join("; ")}.
            </span>
          </div>
        )}
        {(["historical_unattributed", "historical_without_run", "inconsistent_run"].includes(evaluation.driftStatus) || evaluation.unattributedDriftSignals > 0) && (
          <div className="mb-5 px-3 py-3 border flex items-start gap-2 text-sm"
            style={{ borderColor: "#6b5529", background: C.amberBg, color: C.text, borderRadius: 3 }}>
            <AlertTriangle size={16} style={{ color: C.amber, marginTop: 1, flexShrink: 0 }} />
            <span>
              {evaluation.driftStatus === "historical_unattributed"
                ? "Historical drift signals have no evaluator fingerprint, so Verdict cannot attribute them to the selected evaluator. They are excluded; this is unavailable evidence, not zero drift."
                : evaluation.driftStatus === "historical_without_run"
                  ? "Historical drift signals predate run snapshots. They are excluded because Verdict cannot prove that they belong to the latest completed analysis; this is unavailable evidence, not zero drift."
                  : evaluation.driftStatus === "inconsistent_run"
                    ? "The latest drift run is incomplete or inconsistent. Its signals are excluded until the pipeline reruns successfully."
                : `${evaluation.unattributedDriftSignals} historical drift signal${evaluation.unattributedDriftSignals === 1 ? "" : "s"} without evaluator identity ${evaluation.unattributedDriftSignals === 1 ? "is" : "are"} excluded.`}
            </span>
          </div>
        )}
        {tab === "overview" && <Overview data={DATA} />}
        {tab === "drift" && <Drift data={DATA} onOpenOperations={operationsUrl ? () => setTab("operations") : null} />}
        {tab === "traces" && <Traces key={evaluation.selectedId || "none"} data={DATA} />}
        {tab === "registry" && <Registry url={mountedRegistryUrl()} operationsUrl={operationsUrl} />}
        {tab === "judge" && <Judge data={DATA} onOpenOperations={operationsUrl ? () => setTab("operations") : null} />}
        {tab === "compare" && <Compare data={DATA} source={source} />}
        {tab === "operations" && operationsUrl && <Operations url={operationsUrl} costBreakdown={DATA.meta.costBreakdown} />}
      </main>
    </div>
  );
}

function MetricCell({ label, value, sub, icon: Icon, accent }) {
  return (
    <div className="px-4 py-5 min-w-0" style={{ background: "transparent" }}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-mono" style={{ color: C.sub }}>{label}</span>
        {Icon && <Icon size={15} style={{ color: accent || C.faint }} />}
      </div>
      <div className="mt-2 font-semibold" style={{ fontSize: 24, color: accent || C.text }}>{value}</div>
      {sub && <div className="text-xs mt-0.5" style={{ color: C.faint }}>{sub}</div>}
    </div>
  );
}

function Overview({ data = SEED }) {
  const DATA = data;
  const m = DATA.meta;
  const health = DATA.clusterHealth || { status: "empty", messages: [], minSampleSize: 30, clustersMeetingSampleFloor: 0, nClusters: 0 };
  const healthColor = health.status === "ready" ? C.green : health.status === "fragmented" ? C.red : C.amber;
  const healthLabel = health.status === "ready" ? "Ready" : health.status === "fragmented" ? "Fragmented" : "Needs volume";
  const leadSignal = DATA.driftSignals[0];
  const leadIsCoverage = leadSignal?.statName === "unclear_rate_increase";
  const leadIsImprovement = leadSignal?.direction === "improvement";
  const leadColor = leadIsImprovement ? C.green : C.red;
  const leadBg = leadIsImprovement ? C.greenBg : C.redBg;
  const analysis = driftAnalysis(DATA);
  const noCompletedRun = analysis.runStatus === "no_completed_run";
  const evaluatorSelectionNeeded = ["selection_required", "invalid_selection"].includes(analysis.runStatus);
  const analysisUnavailable = noCompletedRun || evaluatorSelectionNeeded;
  const completedWithSignals = analysis.runStatus === "completed_with_signals";
  const statusColor = analysisUnavailable ? C.amber : completedWithSignals ? leadColor : C.green;
  const statusBg = analysisUnavailable ? C.amberBg : completedWithSignals ? leadBg : C.greenBg;
  const seriesColors = [C.accent, C.accent2, C.amber, C.green, C.blue, C.cyan];
  const providerSeries = DATA.providers.map((provider, index) => {
    const presentation = providerPresentation(
      provider.rawProvider ?? provider.key,
      provider.model,
      provider.label,
    );
    return {
      key: provider.key,
      label: presentation.label,
      color: presentation.color || seriesColors[index % seriesColors.length],
    };
  });
  const clusterMode = providerSeries.length === 1
    && DATA.clusters.length > 1
    && (DATA.clusterPassrate || []).length > 0;
  const driftClusters = new Set(DATA.driftSignals.map((signal) => signal.clusterId).filter(Boolean));
  const clusterSeries = DATA.clusters.map((cluster, index) => ({
    key: cluster.cluster_id,
    label: cluster.cluster_id,
    color: driftClusters.has(cluster.cluster_id) ? C.red : seriesColors[(index + 1) % seriesColors.length],
  }));
  const qualitySeries = clusterMode ? clusterSeries : providerSeries;
  const qualityRows = clusterMode ? DATA.clusterPassrate : DATA.passrate;
  return (
    <div className="space-y-5">
      <div className="border-y flex flex-col lg:flex-row" style={{ borderColor: C.border, background: C.panel2 }}>
        <div className="px-4 py-4 flex items-start gap-3 flex-1">
          <div className="w-8 h-8 flex items-center justify-center shrink-0" style={{ background: statusBg, borderRadius: 3 }}>
            {analysisUnavailable ? <Clock size={16} style={{ color: C.amber }} /> : completedWithSignals ? leadIsImprovement ? <TrendingUp size={16} style={{ color: leadColor }} /> : <AlertTriangle size={16} style={{ color: leadColor }} /> : <CheckCircle2 size={16} style={{ color: C.green }} />}
          </div>
          <div className="min-w-0">
            <div className="text-xs font-mono" style={{ color: statusColor }}>{analysisUnavailable ? "DRIFT ANALYSIS" : `LATEST COMPLETED RUN${DATA.driftRun?.analysisTime ? ` · ${DATA.driftRun.analysisTime}` : ""}`}</div>
            <div className="font-semibold mt-0.5">{analysisHeadline(analysis)}</div>
            <div className="text-sm mt-0.5" style={{ color: C.sub }}>
              {evaluatorSelectionNeeded
                ? "Choose one evaluator identity to view its completed drift run."
                : noCompletedRun
                ? `No completed run · current ${analysis.current}/${analysis.minimum} · baseline ${analysis.baseline}/${analysis.minimum}.`
                : completedWithSignals
                  ? `${DATA.driftSignals.length} persisted signal${DATA.driftSignals.length === 1 ? "" : "s"}: ${DATA.driftSignals.map((s) => dimensionLabel(s.dimension)).join(" and ")}.`
                  : "The latest completed run produced zero signals."}
            </div>
          </div>
        </div>
        <div className="grid grid-cols-3 border-t lg:border-t-0 lg:border-l" style={{ borderColor: C.border, minWidth: "min(100%, 430px)" }}>
          <GateCell label="ADJUSTED P" value={analysisUnavailable ? "—" : leadIsCoverage ? "n/a" : leadSignal ? sci(leadSignal.pAdj) : "clear"} color={analysisUnavailable || leadIsCoverage ? C.amber : leadSignal ? C.red : C.green} />
          <GateCell label="CLIFF'S DELTA" value={analysisUnavailable ? "—" : leadIsCoverage ? "n/a" : leadSignal ? statValue(leadSignal.cliffsDelta) : "clear"} color={analysisUnavailable || leadIsCoverage ? C.amber : leadSignal ? C.red : C.green} />
          <GateCell label="CURRENT N" value={leadSignal?.nCur ?? "—"} color={C.text} />
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-px border-y" style={{ borderColor: C.border, background: C.border }}>
        <MetricCell label="Traces captured" value={m.totalTraces.toLocaleString()} sub={`${m.providers} providers`} icon={Activity} accent={C.accent} />
        <MetricCell label="Responses judged" value={m.totalJudged} sub={`${DATA.dimensionOverall.length}-dimension rubric`} icon={Scale} accent={C.blue} />
        <MetricCell label="Drift signals" value={evaluatorSelectionNeeded ? "Select evaluator" : noCompletedRun ? "Not run" : DATA.driftSignals.length} sub={analysisUnavailable ? analysisHeadline(analysis).toLowerCase() : "latest completed run"} icon={Signal} accent={analysisUnavailable ? C.amber : DATA.driftSignals.length ? C.red : C.green} />
        <MetricCell label="Estimated spend" value={usd(m.totalCost)} sub={m.totalCostStatus === "partial" ? "partial pricing coverage" : m.totalCostStatus === "unavailable" ? "pricing unavailable" : `${m.durationHours} hour window`} icon={DollarSign} accent={m.totalCostStatus === "complete" ? C.green : C.amber} />
        <MetricCell label="Intent clusters" value={m.clusters} sub={`median ${health.medianClusterSize || 0} traces`} icon={Layers} accent={healthColor} />
        <MetricCell label="Cluster readiness" value={healthLabel} sub={`${health.clustersMeetingSampleFloor}/${health.nClusters} meet n=${health.minSampleSize}`} icon={Gauge} accent={healthColor} />
      </div>

      {health.messages && health.messages.length > 0 && (
        <div className="px-4 py-3 border flex items-start gap-2 text-sm" style={{ borderColor: healthColor, background: C.amberBg, color: C.text, borderRadius: 3 }}>
          <AlertTriangle size={16} style={{ color: healthColor, marginTop: 1, flexShrink: 0 }} />
          <span>{health.messages.join(" ")}</span>
        </div>
      )}

      <div className="grid lg:grid-cols-12 gap-5">
        <Panel className="p-5 lg:col-span-8">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-1">
            <div className="font-semibold text-sm">Judge pass-rate over time</div>
            <SeriesLegend series={qualitySeries} />
          </div>
          <div className="text-xs mb-3" style={{ color: C.sub }}>
            {clusterMode ? "Pass rate by intent cluster · single-provider store" : "Pass rate by provider · current capture window"}
          </div>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={qualityRows} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="hour" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(h) => `${h}h`} />
              <YAxis domain={[0, 100]} tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}%`} />
              <Tooltip content={<ChartTooltip unit="%" />} />
              <ReferenceLine x={m.regressionHour} stroke={C.red} strokeDasharray="4 4" label={{ value: "change point", fill: C.red, fontSize: 11, position: "top" }} />
              {qualitySeries.map((series) => (
                <Line key={series.key} type="monotone" dataKey={series.key} name={series.label} stroke={series.color} strokeWidth={2.4} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </Panel>

        <Panel className="p-5 lg:col-span-4" style={{ background: C.panel2 }}>
          <div className="flex items-start justify-between gap-3 mb-1">
            <div>
              <div className="font-semibold text-sm">Intent readiness</div>
              <div className="text-xs mt-1" style={{ color: C.sub }}>Stable assignment volume by cluster</div>
            </div>
            <Pill color={healthColor} bg={health.status === "ready" ? C.greenBg : C.amberBg}>{healthLabel}</Pill>
          </div>
          <div className="grid grid-cols-2 gap-px my-4" style={{ background: C.border }}>
            <div className="p-3" style={{ background: C.panel }}>
              <div className="text-xs font-mono" style={{ color: C.faint }}>READY CLUSTERS</div>
              <div className="font-semibold mt-1">{health.clustersMeetingSampleFloor}/{health.nClusters}</div>
            </div>
            <div className="p-3" style={{ background: C.panel }}>
              <div className="text-xs font-mono" style={{ color: C.faint }}>SAMPLE FLOOR</div>
              <div className="font-semibold mt-1">n={health.minSampleSize}</div>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={188}>
            <BarChart data={DATA.clusters} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="cluster_id" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} width={92} />
              <Tooltip content={<ChartTooltip title=" " />} cursor={{ fill: "rgba(78,225,170,0.05)" }} />
              <Bar dataKey="n" name="traces" radius={[0, 2, 2, 0]} fill={C.accent} isAnimationActive={false}>
                {DATA.clusters.map((_, i) => <Cell key={i} fill={i % 2 ? C.accent2 : C.accent} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Panel>
      </div>

      <Panel className="p-5">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-3">
          <div className="font-semibold text-sm">Mean latency by provider</div>
          <SeriesLegend series={providerSeries} />
        </div>
        <ResponsiveContainer width="100%" height={210}>
          <AreaChart data={DATA.tsRows} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
            <defs>
              {providerSeries.map((series) => (
                <linearGradient key={series.key} id={`g-${series.key}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={series.color} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={series.color} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid stroke={C.grid} vertical={false} />
            <XAxis dataKey="hour" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(h) => `${h}h`} />
            <YAxis tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}s`} />
            <Tooltip content={<ChartTooltip unit="s" />} />
            {providerSeries.map((series) => (
              <Area key={series.key} type="monotone" dataKey={`${series.key}_lat`} name={series.label} stroke={series.color} strokeWidth={2} fill={`url(#g-${series.key})`} connectNulls isAnimationActive={false} />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </Panel>
    </div>
  );
}

function GateCell({ label, value, color }) {
  return (
    <div className="px-3 py-4 border-r last:border-r-0" style={{ borderColor: C.border }}>
      <div className="text-xs font-mono" style={{ color: C.faint }}>{label}</div>
      <div className="font-semibold mt-1" style={{ color, fontSize: 16 }}>{value}</div>
    </div>
  );
}

function SeriesLegend({ series }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" style={{ color: C.sub }}>
      {series.map((item) => (
        <span key={item.key} className="flex items-center gap-1.5"><Dot color={item.color} />{item.label}</span>
      ))}
    </div>
  );
}

/* ----------------------------------------------------------------- DRIFT */
function Drift({ data = SEED, onOpenOperations = null }) {
  const DATA = data;
  const [open, setOpen] = useState(DATA.driftSignals[0]?.id);
  const analysis = driftAnalysis(DATA);
  const evaluatorSelectionNeeded = ["selection_required", "invalid_selection"].includes(analysis.runStatus);
  const seriesColors = [C.accent, C.accent2, C.amber, C.green, C.blue, C.cyan];
  const dimensionSeries = DATA.dimensionOverall.map((dimension, index) => ({
    key: dimension.dim,
    label: dimensionLabel(dimension.dim),
    color: seriesColors[index % seriesColors.length],
  }));
  return (
    <div className="space-y-4">
      <div className="text-sm" style={{ color: C.sub }}>
        PASS/FAIL signals clear both the BH-adjusted p-value and minimum absolute Cliff&apos;s δ gates.
        UNCLEAR coverage signals instead use a deterministic 15-point increase and the configured sample floor. Signals come from the latest persisted pipeline run.
      </div>
      {(analysis.runStatus === "no_completed_run" || evaluatorSelectionNeeded) && (
        <Panel className="p-5">
          <div className="flex items-start gap-3">
            <Clock size={18} style={{ color: C.amber, marginTop: 1 }} />
            <div className="flex-1">
              <div className="font-semibold">{evaluatorSelectionNeeded ? "Select an evaluator to view drift analysis" : "No drift analysis has completed yet"}</div>
              <div className="text-sm mt-1" style={{ color: C.sub }}>
                {evaluatorSelectionNeeded
                  ? "Choose an evaluator identity above to load its completed runs and persisted signals."
                  : `${analysisHeadline(analysis)}. Capturing new content-bearing traces advances the current window first; the same traces cannot also fill the historical baseline.`}
              </div>
              {ReadinessSummary({ analysis })}
              {onOpenOperations && (
                <button onClick={onOpenOperations} className="mt-4 inline-flex items-center gap-2 text-sm font-semibold px-3 py-2"
                  style={{ background: C.accent, color: C.bg, borderRadius: 3 }}>
                  Open Operations
                </button>
              )}
            </div>
          </div>
        </Panel>
      )}
      {analysis.runStatus === "completed_no_signals" && (
        <Panel className="p-4 flex items-start gap-3">
          <CheckCircle2 size={18} style={{ color: C.green, marginTop: 1 }} />
          <div>
            <div className="font-semibold">Completed with no signals</div>
            <div className="text-sm mt-1" style={{ color: C.sub }}>The latest completed run evaluated its eligible cells and none cleared the configured alert gates.</div>
          </div>
        </Panel>
      )}
      {DATA.driftSignals.map((s) => {
        const isOpen = open === s.id;
        const isCoverage = s.statName === "unclear_rate_increase";
        const isImprovement = s.direction === "improvement";
        const signalColor = isImprovement ? C.green : C.red;
        const signalBg = isImprovement ? C.greenBg : C.redBg;
        return (
          <Panel key={s.id} className="overflow-hidden">
            <button onClick={() => setOpen(isOpen ? null : s.id)} className="w-full flex items-center gap-4 p-4 text-left">
              <div className="w-10 h-10 flex items-center justify-center shrink-0" style={{ background: signalBg, borderRadius: 3 }}>
                {isImprovement ? <TrendingUp size={19} style={{ color: signalColor }} /> : <TrendingDown size={19} style={{ color: signalColor }} />}
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-semibold">{dimensionLabel(s.dimension)}</span>
                  <Pill color={signalColor} bg={signalBg}>{isCoverage ? "evaluability regression" : s.direction}</Pill>
                  <span className="text-xs" style={{ color: C.sub }}>on {s.providerLabel}</span>
                </div>
                <div className="text-xs mt-0.5" style={{ color: C.sub }}>
                  {isCoverage ? <>UNCLEAR rate = {coveragePct(s.stat)} · deterministic coverage gate</> : <>{s.statName === "fisher_exact" ? "Fisher's exact" : "Mann-Whitney U"} · p<sub>adj</sub> = {sci(s.pAdj)} · {s.cliffsDelta != null ? <>Cliff&apos;s δ = {s.cliffsDelta} · </> : null}Cohen&apos;s d = {statValue(s.cohensD)}</>}
                </div>
              </div>
              <div className="ml-auto flex items-center gap-3">
                <div className="text-right">
                  <div className="font-semibold" style={{ color: signalColor, fontSize: 18 }}>{isCoverage ? coveragePct(s.stat) : statValue(s.cliffsDelta ?? s.cohensD)}</div>
                  <div className="text-xs" style={{ color: C.faint }}>{isCoverage ? "UNCLEAR rate" : s.cliffsDelta != null ? "Cliff's δ" : "effect size"}</div>
                </div>
                <ChevronRight size={18} style={{ color: C.faint, transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }} />
              </div>
            </button>
            {isOpen && (
              <div className="px-4 pb-4 border-t" style={{ borderColor: C.border }}>
                <div className="grid sm:grid-cols-4 gap-3 mt-4">
                  <Stat label="Statistic" value={isCoverage ? coveragePct(s.stat) : s.statName === "fisher_exact" ? `OR = ${statValue(s.stat)}` : `U = ${statValue(s.stat)}`} note={isCoverage ? "current UNCLEAR fraction" : s.statName === "fisher_exact" ? "Fisher's exact" : "Mann-Whitney"} />
                  <Stat label="p-value" value={isCoverage ? "n/a" : sci(s.p)} note={isCoverage ? "deterministic threshold" : `adjusted ${sci(s.pAdj)} (BH)`} />
                  <Stat label="Cliff's δ" value={isCoverage ? "n/a" : statValue(s.cliffsDelta)} note={isCoverage ? "not a PASS/FAIL test" : "primary effect-size gate"} />
                  <Stat label="Samples" value={`${statValue(s.nCur)} vs ${statValue(s.nBase)}`} note="current vs baseline" />
                </div>
                <div className="mt-4 rounded-md p-3 flex items-start gap-2.5" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
                  <Zap size={15} style={{ color: C.amber, marginTop: 1 }} />
                  <div>
                    <div className="text-xs font-medium" style={{ color: C.amber }}>Recommended action</div>
                    <div className="text-sm" style={{ color: C.text }}>{s.action}</div>
                  </div>
                </div>
                <div className="mt-3 flex items-center gap-2 text-xs" style={{ color: C.faint }}>
                  <span>Contributing layer:</span>
                  {s.layers.map((l) => <Pill key={l} color={C.sub}>{l}</Pill>)}
                  <span className="ml-auto font-mono">signal {s.id.slice(0, 8)}</span>
                </div>
                {s.exampleTraceIds && s.exampleTraceIds.length > 0 && (
                  <div className="mt-3 pt-3 border-t text-xs" style={{ borderColor: C.border }}>
                    <span style={{ color: C.faint }}>Evidence traces</span>
                    <div className="flex flex-wrap gap-1.5 mt-1.5">
                      {s.exampleTraceIds.map((traceId) => <Pill key={traceId} color={C.accent}><span title={traceId}>{shortTraceId(traceId)}</span></Pill>)}
                    </div>
                  </div>
                )}
              </div>
            )}
          </Panel>
        );
      })}

      {analysis.runStatus !== "no_completed_run" && <Panel className="p-5">
        <div className="font-semibold text-sm mb-1">Focused provider pass rate by dimension</div>
        <div className="text-xs mb-3" style={{ color: C.sub }}>
          {DATA.focusProviderLabel ? `${DATA.focusProviderLabel} is shown. Mixed-provider lead signals fall back to the first available provider rather than an empty chart.` : "No provider series is available."}
        </div>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={DATA.haikuDim} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid stroke={C.grid} vertical={false} />
            <XAxis dataKey="hour" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(h) => `${h}h`} />
            <YAxis domain={[0, 100]} tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}%`} />
            <Tooltip content={<ChartTooltip unit="%" />} />
            <ReferenceArea x1={DATA.meta.regressionHour} x2={DATA.meta.regressionHour + 3} fill={C.red} fillOpacity={0.06} />
            <ReferenceLine x={DATA.meta.regressionHour} stroke={C.red} strokeDasharray="4 4" />
            {dimensionSeries.map((dimension) => (
              <Line key={dimension.key} type="monotone" dataKey={dimension.key} name={dimension.label} stroke={dimension.color} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-3 text-xs" style={{ color: C.sub }}>
          {dimensionSeries.map((dimension) => (
            <span key={dimension.key} className="flex items-center gap-1.5"><Dot color={dimension.color} />{dimension.label}</span>
          ))}
        </div>
      </Panel>}
    </div>
  );
}

function Stat({ label, value, note }) {
  return (
    <div className="rounded-md p-3" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
      <div className="text-xs" style={{ color: C.sub }}>{label}</div>
      <div className="font-semibold mt-0.5" style={{ fontSize: 17 }}>{value}</div>
      {note && <div className="text-xs mt-0.5" style={{ color: C.faint }}>{note}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- TRACES */
function Traces({ data = SEED }) {
  const DATA = data;
  const [prov, setProv] = useState("all");
  const [capture, setCapture] = useState("all");
  const [q, setQ] = useState("");
  const [selectedTraceId, setSelectedTraceId] = useState(null);
  const sel = DATA.samples.find((sample) => sample.trace_id === selectedTraceId) || null;
  const providerOptions = DATA.providers.map((provider) => {
    const presentation = providerPresentation(
      provider.rawProvider ?? provider.key,
      provider.model,
      provider.label,
    );
    return { key: provider.key, label: presentation.short };
  });
  const rows = DATA.samples.filter((s) =>
    (prov === "all" || (s.providerKey || s.provider) === prov) &&
    (capture === "all" || (capture === "captured") === capturedContent(s)) &&
    (q === "" || (s.prompt_redacted || "").toLowerCase().includes(q.toLowerCase()) ||
      (s.trace_id || "").toLowerCase().includes(q.toLowerCase()))
  );
  const traceResource = DATA.truncation?.resources?.traceSamples;
  const shown = traceResource?.shown ?? DATA.samples.length;
  const available = traceResource?.available ?? DATA.meta.totalTraces ?? DATA.samples.length;

  return (
    <div className="flex flex-col xl:flex-row gap-5">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <div className="flex items-center gap-2 px-3 py-1.5 border flex-1" style={{ borderColor: C.border, background: C.panel, minWidth: 180, borderRadius: 3 }}>
            <Search size={14} style={{ color: C.faint }} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search prompt or trace ID…"
              className="bg-transparent outline-none text-sm flex-1" style={{ color: C.text }} />
          </div>
          <div className="flex items-center gap-1 px-1 py-1 border" style={{ borderColor: C.border, background: C.panel, borderRadius: 3 }}>
            <Filter size={13} style={{ color: C.faint, marginLeft: 4 }} />
            {[{ key: "all", label: "All providers" }, ...providerOptions].map((option) => (
              <button key={option.key} onClick={() => setProv(option.key)} className="text-xs px-2.5 py-1 rounded-md"
                style={{ background: prov === option.key ? C.raised : "transparent", color: prov === option.key ? C.text : C.sub, fontWeight: prov === option.key ? 600 : 400 }}>
                {option.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 px-1 py-1 border" style={{ borderColor: C.border, background: C.panel, borderRadius: 3 }}>
            {[
              { key: "all", label: "All" },
              { key: "captured", label: "Content captured" },
              { key: "metadata", label: "Metadata only" },
            ].map((option) => (
              <button key={option.key} onClick={() => setCapture(option.key)} className="text-xs px-2.5 py-1 rounded-md"
                style={{ background: capture === option.key ? C.raised : "transparent", color: capture === option.key ? C.text : C.sub, fontWeight: capture === option.key ? 600 : 400 }}>
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <Panel className="overflow-x-auto">
          <div style={{ minWidth: 840 }}>
          <div className="grid text-xs px-4 py-2.5 border-b" style={{ borderColor: C.border, color: C.sub, gridTemplateColumns: "150px 1fr 88px 96px 70px 64px" }}>
            <span>Recorded</span><span>Prompt</span><span>Cluster</span><span>Tokens</span><span>Latency</span><span>Status</span>
          </div>
          <div style={{ maxHeight: 520, overflowY: "auto" }}>
            {rows.map((s) => {
              const on = sel && sel.trace_id === s.trace_id;
              const verdicts = s.judgment ? s.judgment.dims : null;
              const judgmentStatus = s.judgment?.summary?.status || (
                verdicts?.some((d) => d.verdict === "fail") ? "fail"
                  : verdicts?.some((d) => d.verdict === "unclear") ? "unclear"
                    : verdicts ? "pass" : "unavailable"
              );
              const provider = providerPresentation(s.provider, s.request_model);
              const recorded = traceTime(s);
              return (
                <button key={s.trace_id} onClick={() => setSelectedTraceId(s.trace_id)}
                  className="w-full grid items-center text-left px-4 py-2.5 border-b text-sm"
                  style={{ borderColor: C.grid, gridTemplateColumns: "150px 1fr 88px 96px 70px 64px", background: on ? C.raised : "transparent" }}>
                  <span className="text-xs" style={{ color: C.faint }}>
                    <span className="block font-mono">{recorded.absolute}</span>
                    {recorded.relative && <span className="block mt-0.5">{recorded.relative}</span>}
                  </span>
                  <span className="flex items-center gap-2 min-w-0 pr-3">
                    <Dot color={provider.color} />
                    <span className="truncate" style={{ color: C.text }}>
                      {!capturedContent(s) ? "Historical metadata-only trace" : (s.prompt_redacted || "Captured prompt was empty")}
                    </span>
                  </span>
                  <span><Pill color={C.sub}>{s.cluster_id || "Unassigned"}</Pill></span>
                  <span className="text-xs" style={{ color: C.sub }}>{s.input_tokens ?? "—"}/{s.output_tokens ?? "—"}</span>
                  <span className="text-xs" style={{ color: C.sub }}>{s.latency_ms ? `${(s.latency_ms / 1000).toFixed(1)}s` : "—"}</span>
                  <span>
                    {s.error ? <Pill color={C.red} bg={C.redBg}>error</Pill>
                      : judgmentStatus === "fail" ? <Pill color={C.red} bg={C.redBg}>fail</Pill>
                        : judgmentStatus === "unclear" ? <Pill color={C.amber} bg={C.amberBg}>unclear</Pill>
                          : judgmentStatus === "pass" ? <Pill color={C.green} bg={C.greenBg}>pass</Pill>
                          : <Pill color={C.faint}>—</Pill>}
                  </span>
                </button>
              );
            })}
            {rows.length === 0 && <div className="p-8 text-center text-sm" style={{ color: C.faint }}>No traces match.</div>}
          </div>
          </div>
        </Panel>
        <div className="text-xs mt-2" style={{ color: C.faint }}>
          {`Showing newest ${shown.toLocaleString()} of ${available.toLocaleString()} total traces. Store totals are complete. Filters apply to this bounded view. ${rows.length.toLocaleString()} match the current filters.`}
        </div>
      </div>

      {/* detail */}
      <div className="shrink-0" style={{ width: "min(100%, 360px)" }}>
        {sel ? <TraceDetail s={sel} onClose={() => setSelectedTraceId(null)} /> : (
          <Panel className="p-8 text-center">
            <Eye size={22} style={{ color: C.faint, margin: "0 auto" }} />
            <div className="text-sm mt-2" style={{ color: C.sub }}>Select a trace to inspect its metadata and any captured content or judge verdicts.</div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function TraceDetail({ s, onClose }) {
  const provider = providerPresentation(s.provider, s.request_model);
  const recorded = traceTime(s);
  const hasContent = capturedContent(s);
  const hasPrompt = s.prompt_redacted != null;
  const hasResponse = s.response_redacted != null;
  const contentLabel = hasPrompt && hasResponse
    ? "Content captured"
    : hasContent
      ? "Content partially captured"
      : "Historical metadata-only trace";
  return (
    <Panel className="overflow-hidden sticky top-20">
      <div className="flex items-center justify-between px-4 py-3 border-b" style={{ borderColor: C.border }}>
        <div className="flex items-center gap-2">
          <Dot color={provider.color} />
          <span className="font-semibold text-sm">{provider.label}</span>
        </div>
        <button onClick={onClose}><X size={16} style={{ color: C.faint }} /></button>
      </div>
      <div className="p-4 space-y-3" style={{ maxHeight: 560, overflowY: "auto" }}>
        <div className="flex flex-wrap gap-1.5">
          <Pill color={C.sub}>{s.cluster_id || "Unassigned"}</Pill>
          <Pill color={hasContent ? C.green : C.amber} bg={hasContent ? C.greenBg : C.amberBg}>{contentLabel}</Pill>
          <Pill color={s.error ? C.red : C.green} bg={s.error ? C.redBg : C.greenBg}>{s.error ? "Failed trace" : "Provider succeeded"}</Pill>
          <Pill color={s.judgment ? C.blue : C.faint}>{s.judgment ? "Judge results available" : "No judge results"}</Pill>
        </div>
        <div className="text-xs font-mono" style={{ color: C.sub }}>{recorded.absolute}{recorded.relative ? ` · ${recorded.relative}` : ""}</div>
        <div className="text-xs" style={{ color: C.faint }}>
          Provider: <span style={{ color: C.sub }}>{s.provider == null || s.provider === "" ? "<unknown>" : String(s.provider)}</span>
          {" · "}Model: <span style={{ color: C.sub }}>{s.request_model == null || s.request_model === "" ? "<unknown>" : String(s.request_model)}</span>
        </div>
        {!hasContent && (
          <div className="text-sm p-3" style={{ background: C.amberBg, color: C.text, border: `1px solid #6b5529`, borderRadius: 3 }}>
            Prompt and response content were not captured when this trace was recorded.
          </div>
        )}
        <div>
          <div className="text-xs mb-1" style={{ color: C.faint }}>PROMPT</div>
          <div className="text-sm p-2.5" style={{ background: C.panel2, color: C.text, border: `1px solid ${C.border}`, borderRadius: 3 }}>
            {s.prompt_redacted == null ? (hasContent ? "Prompt was not captured for this trace." : "Not available for this historical metadata-only trace.") : (s.prompt_redacted || "Captured prompt was empty.")}
          </div>
        </div>
        <div>
          <div className="text-xs mb-1" style={{ color: C.faint }}>RESPONSE</div>
          <div className="text-sm p-2.5" style={{ background: C.panel2, color: C.sub, border: `1px solid ${C.border}`, borderRadius: 3, lineHeight: 1.5 }}>
            {s.response_redacted == null ? (hasContent ? "Response was not captured for this trace." : "Not available for this historical metadata-only trace.") : (s.response_redacted || "Captured response was empty.")}
          </div>
        </div>
        {s.error && (
          <div className="text-sm p-2.5" style={{ background: C.redBg, color: C.red, border: `1px solid ${C.red}`, borderRadius: 3 }}>
            Provider failure: {s.error}
          </div>
        )}
        {s.judgment && (
          <div>
            <div className="text-xs mb-1.5 flex items-center justify-between">
              <span style={{ color: C.faint }}>JUDGE VERDICTS</span>
              <span style={{ color: C.faint }}>{s.judgment.judges.join(", ")}</span>
            </div>
            <div className="space-y-2">
              {s.judgment.dims.map((d) => (
                <div key={d.name} className="p-2.5" style={{ background: C.panel2, border: `1px solid ${C.border}`, borderRadius: 3 }}>
                  <div className="flex items-center gap-2">
                    {d.verdict === "pass" ? <CheckCircle2 size={14} style={{ color: C.green }} />
                      : d.verdict === "fail" ? <X size={14} style={{ color: C.red }} />
                        : <AlertTriangle size={14} style={{ color: C.amber }} />}
                    <span className="text-sm font-medium">{dimensionLabel(d.name)}</span>
                    <span className="ml-auto text-xs font-semibold" style={{ color: d.verdict === "pass" ? C.green : d.verdict === "fail" ? C.red : C.amber }}>
                      {d.verdict.toUpperCase()}
                    </span>
                  </div>
                  {d.reasoning && <div className="text-xs mt-1.5" style={{ color: C.sub, lineHeight: 1.45 }}>{d.reasoning}</div>}
                </div>
              ))}
            </div>
          </div>
        )}
        {!s.judgment && (
          <div className="text-sm p-2.5" style={{ background: C.panel2, color: C.sub, border: `1px solid ${C.border}`, borderRadius: 3 }}>
            No judge results are stored for this trace.
          </div>
        )}
        <div className="text-xs font-mono pt-1" style={{ color: C.faint }}>trace {s.trace_id.slice(0, 12)}</div>
      </div>
    </Panel>
  );
}

/* ----------------------------------------------------------------- JUDGE */
function Judge({ data = SEED, onOpenOperations = null }) {
  const DATA = data;
  const providerKeys = DATA.providers.map((provider) => provider.key);
  const providerPresentations = Object.fromEntries(DATA.providers.map((provider) => [
    provider.key,
    providerPresentation(provider.rawProvider ?? provider.key, provider.model, provider.label),
  ]));
  const judgeModels = [...new Set(DATA.samples.flatMap((sample) => sample.judgment?.judges || []))];
  const latestHealth = DATA.evaluatorHealth?.[0];
  const scoreCoverage = DATA.scoreCoverage || {};
  const coverageItems = [
    { label: "PASS", value: scoreCoverage.pass, color: C.green },
    { label: "FAIL", value: scoreCoverage.fail, color: C.red },
    { label: "UNCLEAR", value: scoreCoverage.unclear, color: C.amber },
    { label: "Missing", value: scoreCoverage.missing, color: C.amber },
    { label: "Errors", value: scoreCoverage.error, color: C.red },
    { label: "Evaluable", value: scoreCoverage.evaluable, color: C.accent },
  ];
  const hasJudgeEvidence = (DATA.meta.totalJudged || 0) > 0
    || coverageItems.some((item) => (item.value || 0) > 0)
    || DATA.dimensionOverall.length > 0;
  if (!hasJudgeEvidence) {
    const analysis = driftAnalysis(DATA);
    const evaluatorSelectionNeeded = ["selection_required", "invalid_selection"].includes(analysis.runStatus);
    return (
      <div className="space-y-5">
        <div className="text-sm" style={{ color: C.sub }}>
          Judged responses are scored on configured rubric dimensions, with reasoning stored beside each PASS/FAIL decision.
        </div>
        <Panel className="p-5">
          <div className="flex items-start gap-3">
            <Scale size={18} style={{ color: C.amber, marginTop: 1 }} />
            <div className="flex-1">
              <div className="font-semibold">{evaluatorSelectionNeeded ? "Select an evaluator to view judge results" : "No eligible evaluation pipeline run has completed yet"}</div>
              <div className="text-sm mt-1" style={{ color: C.sub }}>
                {evaluatorSelectionNeeded
                  ? "Choose an evaluator identity above to load its persisted judgments and completed runs."
                  : "Judge charts appear only after an eligible run stores evaluator results. The availability summary uses the same default content-bearing trace windows as drift analysis."}
              </div>
              {ReadinessSummary({ analysis })}
              {onOpenOperations && (
                <button onClick={onOpenOperations} className="mt-4 inline-flex items-center gap-2 text-sm font-semibold px-3 py-2"
                  style={{ background: C.accent, color: C.bg, borderRadius: 3 }}>
                  Open Operations
                </button>
              )}
            </div>
          </div>
        </Panel>
      </div>
    );
  }
  return (
    <div className="space-y-5">
      <div className="text-sm" style={{ color: C.sub }}>
        Judged responses are scored on configured rubric dimensions, with reasoning stored beside each PASS/FAIL decision.
      </div>
      <Panel className="p-4">
        <div className="font-semibold text-sm">Evaluation coverage</div>
        <div className="text-xs mt-1" style={{ color: C.faint }}>
          PASS and FAIL are evaluable labels. UNCLEAR, missing dimensions, and judge errors remain visible rather than entering pass-rate denominators.
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mt-3">
          {coverageItems.map((item) => (
            <div key={item.label} className="rounded-md p-3" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
              <div className="text-xs" style={{ color: C.sub }}>{item.label}</div>
              <div className="font-semibold mt-0.5" style={{ color: item.color, fontSize: 18 }}>{item.value ?? 0}</div>
            </div>
          ))}
        </div>
      </Panel>
      <div className="grid lg:grid-cols-2 gap-5">
        <Panel className="p-5">
          <div className="font-semibold text-sm mb-3">Pass rate by dimension (all providers)</div>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={DATA.dimensionOverall} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="dim" tick={{ fill: C.sub, fontSize: 11 }} stroke={C.border} tickFormatter={dimensionAxisLabel} />
              <YAxis domain={[0, 100]} tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}%`} />
              <Tooltip content={<ChartTooltip unit="%" title=" " />} cursor={{ fill: "rgba(78,225,170,0.05)" }} />
              <Bar dataKey="passRate" name="pass rate" radius={[5, 5, 0, 0]} isAnimationActive={false}>
                {DATA.dimensionOverall.map((d, i) => (
                  <Cell key={i} fill={d.passRate >= 95 ? C.green : d.passRate >= 85 ? C.amber : C.red} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Panel>

        <Panel className="p-5">
          <div className="font-semibold text-sm mb-3">Pass rate by dimension &amp; provider</div>
          <div className="space-y-3 mt-1">
            {DATA.providerDimension.map((row) => (
              <div key={row.dim}>
                <div className="text-xs mb-1.5" style={{ color: C.sub }}>{dimensionLabel(row.dim)}</div>
                <div className="flex gap-2">
                  {providerKeys.map((p) => (
                    <div key={p} className="flex-1">
                      <div className="h-2 rounded-full overflow-hidden" style={{ background: C.grid }}>
                        <div style={{ width: `${row[p] || 0}%`, height: "100%", background: providerPresentations[p].color }} />
                      </div>
                      <div className="flex items-center gap-1 mt-1 text-xs" style={{ color: C.faint }}>
                        <Dot color={providerPresentations[p].color} />
                        <span>{providerPresentations[p].short}</span>
                        <span className="ml-auto">{pct(row[p])}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div className="grid sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {DATA.dimensionOverall.map((d) => (
          <Panel key={d.dim} className="p-4">
            <div className="text-xs" style={{ color: C.sub }}>{dimensionLabel(d.dim)}</div>
            <div className="font-semibold mt-1.5" style={{ fontSize: 22, color: d.passRate == null ? C.faint : d.passRate >= 95 ? C.green : d.passRate >= 85 ? C.amber : C.red }}>{pct(d.passRate)}</div>
            <div className="text-xs mt-0.5" style={{ color: C.faint }}>{d.pass} pass · {d.fail} fail{d.unclear ? ` · ${d.unclear} unclear` : ""}</div>
          </Panel>
        ))}
      </div>

      <Panel className="p-4">
        <div className="flex items-start gap-3">
          <Gauge size={16} style={{ color: latestHealth?.status === "healthy" ? C.green : latestHealth?.status === "degraded" ? C.red : C.amber, marginTop: 1 }} />
          <div className="min-w-0">
            <div className="font-semibold text-sm">Judge health · independent sentinel agreement</div>
            {latestHealth ? (
              <div className="text-sm mt-1" style={{ color: C.sub }}>
                <span style={{ color: C.text }}>{latestHealth.sentinelSetName || "Unnamed anchor set"}</span>: {latestHealth.methodVersion === "1" ? "legacy label-only methodology; unavailable for health gating" : <>{pct(latestHealth.exampleAgreement)} ({latestHealth.correctExamples}/{latestHealth.totalExamples} exact-match examples; 95% CI {pct(latestHealth.exampleConfidenceLow)}–{pct(latestHealth.exampleConfidenceHigh)}). Label agreement: {pct(latestHealth.labelAgreement)} ({latestHealth.correctLabels}/{latestHealth.totalLabels})</>}, status <span style={{ color: latestHealth.status === "healthy" ? C.green : latestHealth.status === "degraded" ? C.red : C.amber }}>{latestHealth.status.replaceAll("_", " ")}</span>{latestHealth.errorCount ? `; ${latestHealth.errorCount} judge errors` : ""}.
              </div>
            ) : (
              <div className="text-sm mt-1" style={{ color: C.sub }}>No fixed human-labeled sentinel run is recorded for this evaluator fingerprint.</div>
            )}
            <div className="text-xs mt-1.5" style={{ color: C.faint }}>Sentinel agreement is separate from production drift and cannot rule out silent judge changes outside the anchor set.</div>
          </div>
        </div>
      </Panel>

      <Panel className="p-4 flex items-start gap-3">
        <Shield size={16} style={{ color: C.accent2, marginTop: 1 }} />
        <div className="text-sm" style={{ color: C.sub }}>
          {judgeModels.length ? (
            <>Judges represented in sampled traces: <span style={{ color: C.text }}>{judgeModels.join(", ")}</span>. </>
          ) : (
            <>Judge model metadata is not available in the sampled traces. </>
          )}
          For real traffic, run the calibration workflow and gate rankings or alerts on workload-specific agreement.
        </div>
      </Panel>
    </div>
  );
}

/* --------------------------------------------------------------- COMPARE */
function Compare({ data = SEED, source = "sample" }) {
  const DATA = data;
  const provs = DATA.providers.map((provider) => ({
    ...provider,
    presentation: providerPresentation(
      provider.rawProvider ?? provider.key,
      provider.model,
      provider.label,
    ),
  }));
  const costData = provs.map((p) => ({ name: p.presentation.short, v: p.cost, key: p.key, color: p.presentation.color }));
  const latData = provs.map((p) => ({ name: p.presentation.short, v: p.avgLatency, key: p.key, color: p.presentation.color }));
  const tokData = provs.map((p) => ({ name: p.presentation.short, v: p.outTok, key: p.key, color: p.presentation.color }));
  const regressedProviders = new Set(
    DATA.driftSignals
      .filter((signal) => signal.direction === "regression" && signal.provider)
      .map((signal) => signal.provider),
  );
  return (
    <div className="space-y-5">
      <div className="text-sm" style={{ color: C.sub }}>
        {source === "sample"
          ? "Bundled synthetic sample: the same prompt set is shown across three providers to demonstrate comparison views. Connect live data before drawing provider conclusions."
          : "Live provider metrics from captured traces. A regression badge appears only when the latest completed drift run attributes a regression to that provider."}
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        {provs.map((p) => {
          const regressed = regressedProviders.has(p.key);
          return (
            <Panel key={p.key} className="p-5" style={{ borderColor: regressed ? "rgba(243,154,98,0.45)" : C.border }}>
              <div className="flex items-center gap-2 mb-3">
                <Dot color={p.presentation.color} />
                <span className="font-semibold">{p.label}</span>
                {regressed && <Pill color={C.red} bg={C.redBg}>regressed</Pill>}
              </div>
              <div className="grid grid-cols-2 gap-y-3 gap-x-2 text-sm">
                <Metric label="Pass rate" value={pct(p.passRate)} color={p.passRate != null && p.passRate >= 95 ? C.green : C.red} />
                <Metric label="Avg latency" value={`${p.avgLatency}s`} />
                <Metric label="Error rate" value={`${p.errorRate}%`} color={p.errorRate > 5 ? C.amber : C.text} />
                <Metric label="Output tokens" value={k(p.outTok)} />
                <Metric label={p.costUnknown ? "Total cost (partial)" : "Total cost"} value={usd(p.cost)} color={p.costUnknown ? C.amber : C.green} />
                <Metric label="Traces" value={p.n.toLocaleString()} />
              </div>
            </Panel>
          );
        })}
      </div>

      <div className="grid md:grid-cols-3 gap-5">
        <MiniBar title="Total cost (8h)" data={costData} fmt={usd} />
        <MiniBar title="Mean latency" data={latData} fmt={(v) => `${v}s`} />
        <MiniBar title="Output tokens (verbosity)" data={tokData} fmt={k} />
      </div>

      <Panel className="p-5">
        <div className="font-semibold text-sm mb-2 flex items-center gap-2"><BarChart3 size={16} style={{ color: C.accent2 }} /> How to read this view</div>
        <ul className="space-y-2 text-sm" style={{ color: C.sub }}>
          <li className="flex gap-2"><span style={{ color: C.amber }}>•</span> <span><span style={{ color: C.text }}>Compare cost and latency on identical traffic</span> before changing providers or prompts.</span></li>
          <li className="flex gap-2"><span style={{ color: C.amber }}>•</span> <span><span style={{ color: C.text }}>Check error rate and output tokens</span> alongside quality signals; drift is rarely one-dimensional.</span></li>
          <li className="flex gap-2"><span style={{ color: C.green }}>•</span> <span><span style={{ color: C.text }}>Use per-cluster views</span> to avoid averaging unlike workloads together.</span></li>
          <li className="flex gap-2"><span style={{ color: C.blue }}>•</span> <span>{source === "sample" ? <><span style={{ color: C.text }}>Treat bundled numbers as sample data</span>; your own traces are the only meaningful provider comparison.</> : <><span style={{ color: C.text }}>Do not treat unmatched traffic as a head-to-head benchmark</span>; compare matched workloads or controlled replays.</>}</span></li>
        </ul>
      </Panel>
    </div>
  );
}

function Metric({ label, value, color }) {
  return (
    <div>
      <div className="text-xs" style={{ color: C.faint }}>{label}</div>
      <div className="font-semibold" style={{ fontSize: 16, color: color || C.text }}>{value}</div>
    </div>
  );
}

function MiniBar({ title, data, fmt }) {
  const known = data.filter((d) => Number.isFinite(d.v));
  const max = Math.max(...known.map((d) => d.v), 1e-12);
  return (
    <Panel className="p-5">
      <div className="font-semibold text-sm mb-3">{title}</div>
      <div className="space-y-2.5">
        {data.map((d) => (
          <div key={d.name}>
            <div className="flex items-center justify-between text-xs mb-1">
              <span className="flex items-center gap-1.5" style={{ color: C.sub }}><Dot color={d.color} />{d.name}</span>
              <span style={{ color: C.text, fontWeight: 600 }}>{fmt(d.v)}</span>
            </div>
            <div className="h-2 rounded-full overflow-hidden" style={{ background: C.grid }}>
              <div style={{ width: `${Number.isFinite(d.v) ? (d.v / max) * 100 : 0}%`, height: "100%", background: d.color }} />
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------- APP */
function App({ data = SEED, source = "sample", onReload, onEvaluatorChange, reloading, loadError, operationsUrl = null }) {
  const [mode, setMode] = useState("landing");
  return (
    <div style={{ fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif", height: "100%", background: C.bg }}>
      {mode === "landing"
        ? <Landing onEnter={() => setMode("dashboard")} />
        : <Dashboard data={data} onExit={() => setMode("landing")} source={source} onReload={onReload}
          onEvaluatorChange={onEvaluatorChange} reloading={reloading} loadError={loadError}
          operationsUrl={operationsUrl} />}
    </div>
  );
}

export function LandingRoot() {
  return (
    <div style={{ fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif", minHeight: "100%", background: C.bg }}>
      <Landing onEnter={() => { window.location.href = "/dashboard"; }} />
    </div>
  );
}

export function DashboardRoot() {
  const data = useDashboardData();
  const operationsUrl = useOperationsConfig();
  return (
    <div style={{ fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif", minHeight: "100%", background: C.bg }}>
      <Dashboard data={data.snapshot} onExit={() => { window.location.href = "/"; }} source={data.source}
        onReload={data.reload} onEvaluatorChange={data.load} reloading={data.loading}
        loadError={data.error} operationsUrl={operationsUrl} />
    </div>
  );
}

export default function Root() {
  const data = useDashboardData();
  const operationsUrl = useOperationsConfig();
  return <App data={data.snapshot} source={data.source} onReload={data.reload}
    onEvaluatorChange={data.load} reloading={data.loading} loadError={data.error} operationsUrl={operationsUrl} />;
}
