import React, { useState, useMemo, useEffect } from "react";
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, ReferenceArea, Cell,
} from "recharts";
import {
  Activity, AlertTriangle, ArrowRight, ArrowLeft, BarChart3, Boxes, CheckCircle2,
  Clock, Code2, Database, GitBranch, Layers, Scale, Search, Shield, Signal,
  TrendingDown, Zap, Github, Terminal, Gauge, FlaskConical, Cpu, DollarSign,
  Filter, X, Sparkles, ChevronRight, Eye, Network, RefreshCw,
} from "lucide-react";

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
      totalCost: 0.42, regressionHour: 4, providers: 3, clusters: 4,
    },
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
    driftSignals: [
      { id: 'sample-01', dimension: 'completeness', direction: 'regression', provider: 'anthropic', providerLabel: 'Anthropic sample', statName: 'fisher_exact', stat: 7.2, p: 0.0009, pAdj: 0.0042, cliffsDelta: -0.42, cohensD: -1.1, nCur: 12, nBase: 12, layers: ['judge_rubric'], action: 'Review affected traces and compare the current prompt/model configuration against the reference window.', detectedAt: 'sample-data' },
      { id: 'sample-02', dimension: 'instruction_following', direction: 'regression', provider: 'anthropic', providerLabel: 'Anthropic sample', statName: 'fisher_exact', stat: 5.8, p: 0.0021, pAdj: 0.0095, cliffsDelta: -0.35, cohensD: -0.9, nCur: 12, nBase: 12, layers: ['judge_rubric'], action: 'Inspect sampled failures and confirm whether the change is meaningful for this workload.', detectedAt: 'sample-data' },
    ],
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
  };
})();

// API endpoint for live data; overridable via window.VERDICT_API. Falls back to SEED.
const API_URL = (typeof window !== "undefined" && window.VERDICT_API) || "/api/data";
let DATA = SEED;

/* ---------------------------------------------------------------- palette */
const C = {
  bg: "#0a0c10", panel: "#12151c", panel2: "#161a22", raised: "#1b2029",
  border: "#262c38", grid: "#1d232e", text: "#e9ebf0", sub: "#8b94a6",
  faint: "#5c6577", accent: "#7b61ff", accent2: "#a78bfa",
  green: "#34d399", red: "#f87171", amber: "#fbbf24", blue: "#60a5fa",
};
const PROV = {
  anthropic: { color: "#e08653", label: "Anthropic sample", short: "Anthropic" },
  openai: { color: "#19c37d", label: "OpenAI sample", short: "OpenAI" },
  google: { color: "#5b8def", label: "Google sample", short: "Google" },
};
const DIM_LABEL = {
  groundedness: "Groundedness", relevance: "Relevance", completeness: "Completeness",
  safety: "Safety", instruction_following: "Instruction-following",
};

/* ---------------------------------------------------------------- helpers */
const pct = (n) => (n == null ? "—" : `${n}%`);
const usd = (n) => `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`;
const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`);
const sci = (n) => {
  if (n === 0) return "0";
  if (n >= 0.001) return n.toFixed(4);
  const e = Math.floor(Math.log10(n));
  return `${(n / Math.pow(10, e)).toFixed(1)}e${e}`;
};

function ChartTooltip({ active, payload, label, unit = "", title }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div style={{ background: C.raised, border: `1px solid ${C.border}`, borderRadius: 10, padding: "8px 11px" }}>
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
    <div className={`border rounded-2xl ${className}`}
      style={{ background: C.panel, borderColor: C.border, ...style }}>
      {children}
    </div>
  );
}

function Pill({ children, color = C.sub, bg }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full text-xs font-medium px-2 py-0.5"
      style={{ color, background: bg || "transparent", border: bg ? "none" : `1px solid ${C.border}` }}>
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
        <rect width="32" height="32" rx="8" fill={C.accent} />
        <path d="M9 10.5 L16 22 L23 10.5" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="16" cy="22.5" r="1.7" fill="#fff" />
      </svg>
    </div>
  );
}

/* ================================================================ LANDING */
function Landing({ onEnter }) {
  const problems = [
    { icon: TrendingDown, title: "Model behavior changes", body: "Provider, model, or prompt changes can shift response quality. Verdict helps surface those changes by workload." },
    { icon: DollarSign, title: "Cost changes need context", body: "Prompt and model changes can alter token usage across agent steps. Verdict tracks cost and latency alongside quality." },
    { icon: Network, title: "Model comparisons take work", body: "Comparing providers on your own traffic usually requires custom logging, labeling, and analysis." },
  ];
  const steps = [
    { icon: Activity, t: "Capture", d: "One line of init. wrapt monkey-patches the SDKs; every LLM call is captured into Verdict's vendor-neutral Trace schema." },
    { icon: Database, t: "Store", d: "Traces land in SQLite, Postgres, or memory — one Storage protocol behind all three." },
    { icon: Layers, t: "Cluster", d: "A streaming Birch clusterer groups prompts by intent so you compare like with like." },
    { icon: Scale, t: "Judge", d: "A configurable judge model scores each response on 5 binary dimensions, reasoning before its verdict." },
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
      <div className="sticky top-0 z-20 border-b backdrop-blur" style={{ borderColor: C.border, background: "rgba(10,12,16,0.8)" }}>
        <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <Logo />
            <span className="font-semibold tracking-tight" style={{ fontSize: 16 }}>Verdict</span>
            <Pill color={C.sub}>v0 · open source</Pill>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden sm:inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg" style={{ color: C.sub }}>
              <Github size={15} /> Apache 2.0
            </span>
            <button onClick={onEnter} className="inline-flex items-center gap-1.5 text-sm font-medium px-3.5 py-1.5 rounded-lg"
              style={{ background: C.accent, color: "#fff" }}>
              Open live dashboard <ArrowRight size={15} />
            </button>
          </div>
        </div>
      </div>

      {/* hero */}
      <div className="max-w-6xl mx-auto px-6 pt-16 pb-14">
        <div className="max-w-3xl">
          <Pill color={C.accent2} bg="rgba(123,97,255,0.12)"><Sparkles size={12} /> Agent observability · drift detection</Pill>
          <h1 className="mt-5 font-semibold tracking-tight" style={{ fontSize: 46, lineHeight: 1.07 }}>
            Monitor LLM-call quality inside your <span style={{ color: C.accent2 }}>agents.</span>
          </h1>
          <p className="mt-5 text-lg" style={{ color: C.sub, lineHeight: 1.6 }}>
            Verdict instruments your agent with one line, scores every LLM call on a 5-dimension rubric,
            and helps you inspect quality, cost, or behavior changes on
            <span style={{ color: C.text }}> your own traffic</span> instead of relying on generic benchmarks.
          </p>
          <div className="mt-7 flex flex-wrap items-center gap-3">
            <button onClick={onEnter} className="inline-flex items-center gap-2 text-sm font-semibold px-5 py-2.5 rounded-xl"
              style={{ background: C.accent, color: "#fff" }}>
              Explore the live dashboard <ArrowRight size={16} />
            </button>
            <span className="inline-flex items-center gap-2 text-sm px-4 py-2.5 rounded-xl border" style={{ borderColor: C.border, color: C.sub }}>
              <Github size={16} /> Read the source
            </span>
          </div>
          <div className="mt-7 flex flex-wrap gap-x-6 gap-y-2 text-sm" style={{ color: C.faint }}>
            <span className="flex items-center gap-1.5"><CheckCircle2 size={14} style={{ color: C.green }} /> Synthetic sample data included</span>
            <span className="flex items-center gap-1.5"><CheckCircle2 size={14} style={{ color: C.green }} /> 3 providers, 4 intent clusters</span>
            <span className="flex items-center gap-1.5"><CheckCircle2 size={14} style={{ color: C.green }} /> Live API replaces the sample when connected</span>
          </div>
        </div>
      </div>

      {/* problems */}
      <Section title="Three problems every team running agents hits in six months">
        <div className="grid md:grid-cols-3 gap-4">
          {problems.map((p) => (
            <Panel key={p.title} className="p-5">
              <div className="w-9 h-9 rounded-xl flex items-center justify-center mb-3" style={{ background: "rgba(248,113,113,0.12)" }}>
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
                <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: C.raised, border: `1px solid ${C.border}` }}>
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
      <Section title="Verification, not vibes" subtitle="Every claim below is reproducible by running a script in the repo.">
        <div className="grid md:grid-cols-3 gap-4">
          {validation.map((v) => (
            <Panel key={v.l} className="p-5" style={{ background: C.panel2 }}>
              <v.icon size={20} style={{ color: v.color }} />
              <div className="mt-3 font-semibold tracking-tight" style={{ fontSize: 26, color: v.color }}>{v.k}</div>
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
<span style={{ color: C.faint }}>{"# That's it. Use Anthropic normally; every call is captured."}</span>
          </pre>
        </Panel>
      </Section>

      {/* honest limits */}
      <Section title="Honest limits" subtitle="What Verdict v0 does not yet claim.">
        <div className="flex flex-wrap gap-2">
          {limits.map((l) => (
            <span key={l} className="text-sm px-3 py-1.5 rounded-lg border" style={{ borderColor: C.border, color: C.sub, background: C.panel }}>{l}</span>
          ))}
        </div>
      </Section>

      {/* cta */}
      <div className="max-w-6xl mx-auto px-6 pb-20">
        <Panel className="p-8 text-center" style={{ background: "linear-gradient(180deg, #161a26, #12151c)" }}>
          <div className="font-semibold tracking-tight" style={{ fontSize: 26 }}>Explore the sample dashboard</div>
          <div className="text-sm mt-2 max-w-xl mx-auto" style={{ color: C.sub }}>
            Walk through synthetic sample data: drift signals, trace explorer, judge scores, and provider comparison. Connect the dashboard to a Verdict database for live data.
          </div>
          <button onClick={onEnter} className="mt-5 inline-flex items-center gap-2 text-sm font-semibold px-5 py-2.5 rounded-xl"
            style={{ background: C.accent, color: "#fff" }}>
            Open live dashboard <ArrowRight size={16} />
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
      <h2 className="font-semibold tracking-tight mb-1" style={{ fontSize: 22 }}>{title}</h2>
      {subtitle && <p className="text-sm mb-5" style={{ color: C.sub }}>{subtitle}</p>}
      {!subtitle && <div className="mb-5" />}
      {children}
    </div>
  );
}

/* ============================================================== DASHBOARD */
function Dashboard({ onExit, source = "sample", onReload, reloading }) {
  const [tab, setTab] = useState("overview");
  const nav = [
    { id: "overview", label: "Overview", icon: Gauge },
    { id: "drift", label: "Drift signals", icon: Signal, badge: DATA.driftSignals.length },
    { id: "traces", label: "Trace explorer", icon: Activity },
    { id: "judge", label: "Judge scores", icon: Scale },
    { id: "compare", label: "Compare LLMs", icon: GitBranch },
  ];
  return (
    <div className="flex" style={{ background: C.bg, color: C.text, minHeight: "100%" }}>
      {/* sidebar */}
      <aside className="w-60 shrink-0 border-r flex flex-col" style={{ borderColor: C.border, background: C.panel }}>
        <div className="h-14 px-4 flex items-center gap-2.5 border-b" style={{ borderColor: C.border }}>
          <Logo size={24} />
          <span className="font-semibold tracking-tight">Verdict</span>
        </div>
        <div className="px-3 py-3 border-b" style={{ borderColor: C.border }}>
          <div className="text-xs" style={{ color: C.faint }}>SERVICE</div>
          <div className="text-sm font-medium mt-0.5">sample-service</div>
          <div className="flex items-center gap-1.5 mt-1.5 text-xs" style={{ color: C.sub }}>
            <Clock size={12} /> Synthetic sample data
          </div>
        </div>
        <nav className="p-2 flex-1">
          {nav.map((n) => {
            const on = tab === n.id;
            return (
              <button key={n.id} onClick={() => setTab(n.id)}
                className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm mb-0.5"
                style={{ background: on ? C.raised : "transparent", color: on ? C.text : C.sub, fontWeight: on ? 600 : 400 }}>
                <n.icon size={16} style={{ color: on ? C.accent2 : C.sub }} />
                {n.label}
                {n.badge ? (
                  <span className="ml-auto text-xs font-semibold px-1.5 rounded-full" style={{ background: C.red, color: "#fff" }}>{n.badge}</span>
                ) : null}
              </button>
            );
          })}
        </nav>
        <button onClick={onExit} className="m-2 flex items-center gap-2 px-3 py-2 rounded-lg text-sm" style={{ color: C.sub }}>
          <ArrowLeft size={15} /> Back to home
        </button>
      </aside>

      {/* main */}
      <main className="flex-1 min-w-0 overflow-x-hidden">
        <div className="h-14 px-6 flex items-center justify-between border-b sticky top-0 z-10" style={{ borderColor: C.border, background: "rgba(10,12,16,0.85)" }}>
          <div className="font-semibold">{nav.find((n) => n.id === tab).label}</div>
          <div className="flex items-center gap-3 text-xs" style={{ color: C.sub }}>
            <span className="flex items-center gap-1.5" title={source === "live" ? "Connected to live API" : "Showing embedded snapshot (no API reachable)"}>
              <span style={{ width: 7, height: 7, borderRadius: 99, background: source === "live" ? C.green : C.amber }} />
              {source === "live" ? "live" : "sample data"}
            </span>
            <span>{DATA.meta.totalTraces.toLocaleString()} traces</span>
            <button onClick={onReload} disabled={reloading} title="Refresh from API"
              className="flex items-center gap-1.5 px-2 py-1 rounded-lg border"
              style={{ borderColor: C.border, color: C.sub, opacity: reloading ? 0.5 : 1 }}>
              <RefreshCw size={13} style={{ animation: reloading ? "vspin 0.8s linear infinite" : "none" }} /> Refresh
            </button>
          </div>
        </div>
        <div className="p-6">
          <div className="mb-5 flex items-start gap-2 px-3 py-2 rounded-lg text-xs" style={{ border: `1px solid ${C.border}`, background: C.raised, color: C.sub }}>
            <FlaskConical size={14} style={{ color: C.amber, marginTop: 1, flexShrink: 0 }} />
            <span>Bundled synthetic sample data is shown until the dashboard can reach <code>/api/data</code>. Sample numbers are for UI demonstration only and are not a provider benchmark.</span>
          </div>
          {tab === "overview" && <Overview />}
          {tab === "drift" && <Drift />}
          {tab === "traces" && <Traces />}
          {tab === "judge" && <Judge />}
          {tab === "compare" && <Compare />}
        </div>
      </main>
    </div>
  );
}

function KPI({ label, value, sub, icon: Icon, accent }) {
  return (
    <Panel className="p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs" style={{ color: C.sub }}>{label}</span>
        {Icon && <Icon size={15} style={{ color: accent || C.faint }} />}
      </div>
      <div className="mt-2 font-semibold tracking-tight" style={{ fontSize: 25 }}>{value}</div>
      {sub && <div className="text-xs mt-0.5" style={{ color: C.faint }}>{sub}</div>}
    </Panel>
  );
}

function Overview() {
  const m = DATA.meta;
  return (
    <div className="space-y-5">
      {/* alert */}
      <div className="rounded-2xl p-4 flex items-start gap-3 border" style={{ borderColor: "rgba(248,113,113,0.4)", background: "rgba(248,113,113,0.08)" }}>
        <AlertTriangle size={18} style={{ color: C.red, marginTop: 1 }} />
        <div className="flex-1">
          <div className="font-semibold text-sm">{DATA.driftSignals.length} sample drift signals on Anthropic sample</div>
          <div className="text-sm mt-0.5" style={{ color: C.sub }}>
            Completeness and instruction-following changed after hour 4 in the bundled sample data.
          </div>
        </div>
        <Pill color={C.red} bg="rgba(248,113,113,0.15)">regression</Pill>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <KPI label="Traces captured" value={m.totalTraces.toLocaleString()} sub="3 providers" icon={Activity} accent={C.accent2} />
        <KPI label="Responses judged" value={m.totalJudged} sub="5-dim rubric" icon={Scale} accent={C.blue} />
        <KPI label="Drift signals" value={m.regressionHour ? DATA.driftSignals.length : 0} sub="sample data" icon={Signal} accent={C.red} />
        <KPI label="Total spend" value={usd(m.totalCost)} sub="over 8 hours" icon={DollarSign} accent={C.green} />
        <KPI label="Intent clusters" value={m.clusters} sub="Birch streaming" icon={Layers} accent={C.amber} />
        <KPI label="Providers" value={m.providers} sub="parallel traffic" icon={Cpu} accent={C.accent2} />
      </div>

      <div className="grid lg:grid-cols-3 gap-5">
        <Panel className="p-5 lg:col-span-2">
          <div className="flex items-center justify-between mb-1">
            <div className="font-semibold text-sm">Judge pass-rate over time</div>
            <Legend3 />
          </div>
          <div className="text-xs mb-3" style={{ color: C.sub }}>A synthetic regression starts at hour 4. Watch the orange line.</div>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={DATA.passrate} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="hour" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(h) => `${h}h`} />
              <YAxis domain={[0, 100]} tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}%`} />
              <Tooltip content={<ChartTooltip unit="%" />} />
              <ReferenceLine x={4} stroke={C.red} strokeDasharray="4 4" label={{ value: "regression injected", fill: C.red, fontSize: 11, position: "top" }} />
              {Object.keys(PROV).map((p) => (
                <Line key={p} type="monotone" dataKey={p} name={PROV[p].label} stroke={PROV[p].color} strokeWidth={2.4} dot={{ r: 2.5 }} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </Panel>

        <Panel className="p-5">
          <div className="font-semibold text-sm mb-1">Intent clusters</div>
          <div className="text-xs mb-3" style={{ color: C.sub }}>Prompts grouped so drift is compared like-with-like.</div>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={DATA.clusters} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="cluster_id" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} width={92} />
              <Tooltip content={<ChartTooltip title=" " />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
              <Bar dataKey="n" name="traces" radius={[0, 5, 5, 0]} fill={C.accent}>
                {DATA.clusters.map((_, i) => <Cell key={i} fill={i % 2 ? C.accent2 : C.accent} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Panel>
      </div>

      <Panel className="p-5">
        <div className="flex items-center justify-between mb-3">
          <div className="font-semibold text-sm">Mean latency by provider</div>
          <Legend3 />
        </div>
        <ResponsiveContainer width="100%" height={210}>
          <AreaChart data={DATA.tsRows} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
            <defs>
              {Object.keys(PROV).map((p) => (
                <linearGradient key={p} id={`g-${p}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={PROV[p].color} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={PROV[p].color} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid stroke={C.grid} vertical={false} />
            <XAxis dataKey="hour" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(h) => `${h}h`} />
            <YAxis tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}s`} />
            <Tooltip content={<ChartTooltip unit="s" />} />
            {Object.keys(PROV).map((p) => (
              <Area key={p} type="monotone" dataKey={`${p}_lat`} name={PROV[p].label} stroke={PROV[p].color} strokeWidth={2} fill={`url(#g-${p})`} connectNulls />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </Panel>
    </div>
  );
}

function Legend3() {
  return (
    <div className="flex items-center gap-3 text-xs" style={{ color: C.sub }}>
      {Object.keys(PROV).map((p) => (
        <span key={p} className="flex items-center gap-1.5"><Dot color={PROV[p].color} />{PROV[p].short}</span>
      ))}
    </div>
  );
}

/* ----------------------------------------------------------------- DRIFT */
function Drift() {
  const [open, setOpen] = useState(DATA.driftSignals[0]?.dimension);
  return (
    <div className="space-y-4">
      <div className="text-sm" style={{ color: C.sub }}>
        Each signal is one (provider, dimension) test that cleared <span style={{ color: C.text }}>both</span> gates: BH-adjusted p &lt; 0.01 and a large effect size.
        Detected at the hour-8 batch run.
      </div>
      {DATA.driftSignals.map((s) => {
        const isOpen = open === s.dimension;
        return (
          <Panel key={s.id} className="overflow-hidden">
            <button onClick={() => setOpen(isOpen ? null : s.dimension)} className="w-full flex items-center gap-4 p-4 text-left">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0" style={{ background: "rgba(248,113,113,0.12)" }}>
                <TrendingDown size={19} style={{ color: C.red }} />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-semibold">{DIM_LABEL[s.dimension]}</span>
                  <Pill color={C.red} bg="rgba(248,113,113,0.14)">regression</Pill>
                  <span className="text-xs" style={{ color: C.sub }}>on {s.providerLabel}</span>
                </div>
                <div className="text-xs mt-0.5" style={{ color: C.sub }}>
                  {s.statName === "fisher_exact" ? "Fisher's exact" : "Mann-Whitney U"} · p<sub>adj</sub> = {sci(s.pAdj)} · {s.cliffsDelta != null ? <>Cliff&apos;s δ = {s.cliffsDelta} · </> : null}Cohen&apos;s d = {s.cohensD}
                </div>
              </div>
              <div className="ml-auto flex items-center gap-3">
                <div className="text-right">
                  <div className="font-semibold" style={{ color: C.red, fontSize: 18 }}>{s.cliffsDelta != null ? s.cliffsDelta : s.cohensD}</div>
                  <div className="text-xs" style={{ color: C.faint }}>{s.cliffsDelta != null ? "Cliff's δ" : "effect size"}</div>
                </div>
                <ChevronRight size={18} style={{ color: C.faint, transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .15s" }} />
              </div>
            </button>
            {isOpen && (
              <div className="px-4 pb-4 border-t" style={{ borderColor: C.border }}>
                <div className="grid sm:grid-cols-4 gap-3 mt-4">
                  <Stat label="Statistic" value={s.statName === "fisher_exact" ? `OR = ${s.stat}` : `U = ${s.stat}`} note={s.statName === "fisher_exact" ? "Fisher's exact" : "Mann-Whitney"} />
                  <Stat label="p-value" value={sci(s.p)} note={`adjusted ${sci(s.pAdj)} (BH)`} />
                  <Stat label="Cohen's d" value={s.cohensD} note={Math.abs(s.cohensD) > 0.8 ? "large effect" : "medium effect"} />
                  <Stat label="Samples" value={`${s.nCur} vs ${s.nBase}`} note="current vs baseline" />
                </div>
                <div className="mt-4 rounded-xl p-3 flex items-start gap-2.5" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
                  <Zap size={15} style={{ color: C.amber, marginTop: 1 }} />
                  <div>
                    <div className="text-xs font-medium" style={{ color: C.amber }}>Recommended action</div>
                    <div className="text-sm" style={{ color: C.text }}>{s.action}</div>
                  </div>
                </div>
                <div className="mt-3 flex items-center gap-2 text-xs" style={{ color: C.faint }}>
                  <span>Contributing layer:</span>
                  {s.layers.map((l) => <Pill key={l} color={C.sub}>{l}</Pill>)}
                  <span className="ml-auto font-mono">signal {s.id}</span>
                </div>
              </div>
            )}
          </Panel>
        );
      })}

      <Panel className="p-5">
        <div className="font-semibold text-sm mb-1">Sample provider pass-rate by dimension</div>
        <div className="text-xs mb-3" style={{ color: C.sub }}>
          Completeness and instruction-following dip after hour 4 in the synthetic sample. Groundedness and safety stay comparatively stable.
        </div>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={DATA.haikuDim} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid stroke={C.grid} vertical={false} />
            <XAxis dataKey="hour" tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(h) => `${h}h`} />
            <YAxis domain={[0, 100]} tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}%`} />
            <Tooltip content={<ChartTooltip unit="%" />} />
            <ReferenceArea x1={4} x2={7} fill={C.red} fillOpacity={0.06} />
            <ReferenceLine x={4} stroke={C.red} strokeDasharray="4 4" />
            {[["completeness", C.red], ["instruction_following", C.amber], ["relevance", C.accent2], ["groundedness", C.green], ["safety", C.blue]].map(([d, col]) => (
              <Line key={d} type="monotone" dataKey={d} name={DIM_LABEL[d]} stroke={col} strokeWidth={2} dot={false} connectNulls />
            ))}
          </LineChart>
        </ResponsiveContainer>
        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-3 text-xs" style={{ color: C.sub }}>
          {[["completeness", C.red], ["instruction_following", C.amber], ["relevance", C.accent2], ["groundedness", C.green], ["safety", C.blue]].map(([d, col]) => (
            <span key={d} className="flex items-center gap-1.5"><Dot color={col} />{DIM_LABEL[d]}</span>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function Stat({ label, value, note }) {
  return (
    <div className="rounded-xl p-3" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
      <div className="text-xs" style={{ color: C.sub }}>{label}</div>
      <div className="font-semibold mt-0.5" style={{ fontSize: 17 }}>{value}</div>
      {note && <div className="text-xs mt-0.5" style={{ color: C.faint }}>{note}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- TRACES */
function Traces() {
  const [prov, setProv] = useState("all");
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(null);
  const rows = useMemo(() => DATA.samples.filter((s) =>
    (prov === "all" || s.provider === prov) &&
    (q === "" || (s.prompt_redacted || "").toLowerCase().includes(q.toLowerCase()))
  ), [prov, q]);

  return (
    <div className="flex gap-5">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg border flex-1" style={{ borderColor: C.border, background: C.panel, minWidth: 180 }}>
            <Search size={14} style={{ color: C.faint }} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search prompts…"
              className="bg-transparent outline-none text-sm flex-1" style={{ color: C.text }} />
          </div>
          <div className="flex items-center gap-1 px-1 py-1 rounded-lg border" style={{ borderColor: C.border, background: C.panel }}>
            <Filter size={13} style={{ color: C.faint, marginLeft: 4 }} />
            {["all", ...Object.keys(PROV)].map((p) => (
              <button key={p} onClick={() => setProv(p)} className="text-xs px-2.5 py-1 rounded-md"
                style={{ background: prov === p ? C.raised : "transparent", color: prov === p ? C.text : C.sub, fontWeight: prov === p ? 600 : 400 }}>
                {p === "all" ? "All" : PROV[p].short}
              </button>
            ))}
          </div>
        </div>

        <Panel className="overflow-hidden">
          <div className="grid text-xs px-4 py-2.5 border-b" style={{ borderColor: C.border, color: C.sub, gridTemplateColumns: "44px 1fr 88px 96px 70px 64px" }}>
            <span>Hour</span><span>Prompt</span><span>Cluster</span><span>Tokens</span><span>Latency</span><span>Status</span>
          </div>
          <div style={{ maxHeight: 520, overflowY: "auto" }}>
            {rows.map((s) => {
              const on = sel && sel.trace_id === s.trace_id;
              const verdicts = s.judgment ? s.judgment.dims : null;
              const failed = verdicts ? verdicts.some((d) => d.verdict === "fail") : false;
              return (
                <button key={s.trace_id} onClick={() => setSel(s)}
                  className="w-full grid items-center text-left px-4 py-2.5 border-b text-sm"
                  style={{ borderColor: C.grid, gridTemplateColumns: "44px 1fr 88px 96px 70px 64px", background: on ? C.raised : "transparent" }}>
                  <span className="text-xs font-mono" style={{ color: C.faint }}>{s.hour}h</span>
                  <span className="flex items-center gap-2 min-w-0 pr-3">
                    <Dot color={PROV[s.provider].color} />
                    <span className="truncate" style={{ color: C.text }}>{s.prompt_redacted}</span>
                  </span>
                  <span><Pill color={C.sub}>{s.cluster_id}</Pill></span>
                  <span className="text-xs" style={{ color: C.sub }}>{s.input_tokens}/{s.output_tokens}</span>
                  <span className="text-xs" style={{ color: C.sub }}>{s.latency_ms ? `${(s.latency_ms / 1000).toFixed(1)}s` : "—"}</span>
                  <span>
                    {s.error ? <Pill color={C.red} bg="rgba(248,113,113,0.14)">error</Pill>
                      : failed ? <Pill color={C.amber} bg="rgba(251,191,36,0.14)">fail</Pill>
                        : verdicts ? <Pill color={C.green} bg="rgba(52,211,153,0.14)">pass</Pill>
                          : <Pill color={C.faint}>—</Pill>}
                  </span>
                </button>
              );
            })}
            {rows.length === 0 && <div className="p-8 text-center text-sm" style={{ color: C.faint }}>No traces match.</div>}
          </div>
        </Panel>
        <div className="text-xs mt-2" style={{ color: C.faint }}>{rows.length} of {DATA.samples.length} sampled traces</div>
      </div>

      {/* detail */}
      <div className="shrink-0" style={{ width: 340 }}>
        {sel ? <TraceDetail s={sel} onClose={() => setSel(null)} /> : (
          <Panel className="p-8 text-center">
            <Eye size={22} style={{ color: C.faint, margin: "0 auto" }} />
            <div className="text-sm mt-2" style={{ color: C.sub }}>Select a trace to see the captured prompt, response, and judge verdicts.</div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function TraceDetail({ s, onClose }) {
  return (
    <Panel className="overflow-hidden sticky top-20">
      <div className="flex items-center justify-between px-4 py-3 border-b" style={{ borderColor: C.border }}>
        <div className="flex items-center gap-2">
          <Dot color={PROV[s.provider].color} />
          <span className="font-semibold text-sm">{PROV[s.provider].label}</span>
        </div>
        <button onClick={onClose}><X size={16} style={{ color: C.faint }} /></button>
      </div>
      <div className="p-4 space-y-3" style={{ maxHeight: 560, overflowY: "auto" }}>
        <div className="flex flex-wrap gap-1.5">
          <Pill color={C.sub}>{s.cluster_id}</Pill>
          <Pill color={C.sub}>{s.hour}h</Pill>
          <Pill color={C.sub}>{s.input_tokens}/{s.output_tokens} tok</Pill>
          <Pill color={C.sub}>{usd(s.cost_usd || 0)}</Pill>
        </div>
        <div>
          <div className="text-xs mb-1" style={{ color: C.faint }}>PROMPT</div>
          <div className="text-sm rounded-lg p-2.5" style={{ background: C.panel2, color: C.text, border: `1px solid ${C.border}` }}>{s.prompt_redacted}</div>
        </div>
        <div>
          <div className="text-xs mb-1" style={{ color: C.faint }}>RESPONSE</div>
          <div className="text-sm rounded-lg p-2.5" style={{ background: C.panel2, color: C.sub, border: `1px solid ${C.border}`, lineHeight: 1.5 }}>
            {s.error ? <span style={{ color: C.red }}>Error: {s.error}</span> : (s.response_redacted || "—")}
          </div>
        </div>
        {s.judgment && (
          <div>
            <div className="text-xs mb-1.5 flex items-center justify-between">
              <span style={{ color: C.faint }}>JUDGE VERDICTS</span>
              <span style={{ color: C.faint }}>{s.judgment.judges.join(", ")}</span>
            </div>
            <div className="space-y-2">
              {s.judgment.dims.map((d) => (
                <div key={d.name} className="rounded-lg p-2.5" style={{ background: C.panel2, border: `1px solid ${C.border}` }}>
                  <div className="flex items-center gap-2">
                    {d.verdict === "pass" ? <CheckCircle2 size={14} style={{ color: C.green }} />
                      : d.verdict === "fail" ? <X size={14} style={{ color: C.red }} />
                        : <AlertTriangle size={14} style={{ color: C.amber }} />}
                    <span className="text-sm font-medium">{DIM_LABEL[d.name]}</span>
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
        <div className="text-xs font-mono pt-1" style={{ color: C.faint }}>trace {s.trace_id.slice(0, 12)}</div>
      </div>
    </Panel>
  );
}

/* ----------------------------------------------------------------- JUDGE */
function Judge() {
  return (
    <div className="space-y-5">
      <div className="text-sm" style={{ color: C.sub }}>
        Every response is scored on five binary dimensions — the judge writes its reasoning before committing to a PASS/FAIL.
      </div>
      <div className="grid lg:grid-cols-2 gap-5">
        <Panel className="p-5">
          <div className="font-semibold text-sm mb-3">Pass rate by dimension (all providers)</div>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={DATA.dimensionOverall} margin={{ top: 5, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="dim" tick={{ fill: C.sub, fontSize: 11 }} stroke={C.border} tickFormatter={(d) => DIM_LABEL[d].split("-")[0]} />
              <YAxis domain={[0, 100]} tick={{ fill: C.sub, fontSize: 12 }} stroke={C.border} tickFormatter={(v) => `${v}%`} />
              <Tooltip content={<ChartTooltip unit="%" title=" " />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
              <Bar dataKey="passRate" name="pass rate" radius={[5, 5, 0, 0]}>
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
                <div className="text-xs mb-1.5" style={{ color: C.sub }}>{DIM_LABEL[row.dim]}</div>
                <div className="flex gap-2">
                  {Object.keys(PROV).map((p) => (
                    <div key={p} className="flex-1">
                      <div className="h-2 rounded-full overflow-hidden" style={{ background: C.grid }}>
                        <div style={{ width: `${row[p] || 0}%`, height: "100%", background: PROV[p].color }} />
                      </div>
                      <div className="flex items-center gap-1 mt-1 text-xs" style={{ color: C.faint }}>
                        <Dot color={PROV[p].color} />{pct(row[p])}
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
            <div className="text-xs" style={{ color: C.sub }}>{DIM_LABEL[d.dim]}</div>
            <div className="font-semibold mt-1.5" style={{ fontSize: 22, color: d.passRate >= 95 ? C.green : d.passRate >= 85 ? C.amber : C.red }}>{d.passRate}%</div>
            <div className="text-xs mt-0.5" style={{ color: C.faint }}>{d.pass} pass · {d.fail} fail{d.unclear ? ` · ${d.unclear} unclear` : ""}</div>
          </Panel>
        ))}
      </div>

      <Panel className="p-4 flex items-start gap-3">
        <Shield size={16} style={{ color: C.accent2, marginTop: 1 }} />
        <div className="text-sm" style={{ color: C.sub }}>
          Judge shown in the bundled sample: <span style={{ color: C.text }}>sample-judge</span>. For real traffic, run the calibration workflow and gate rankings or alerts on workload-specific agreement.
        </div>
      </Panel>
    </div>
  );
}

/* --------------------------------------------------------------- COMPARE */
function Compare() {
  const provs = DATA.providers;
  const costData = provs.map((p) => ({ name: PROV[p.key].short, v: p.cost, key: p.key }));
  const latData = provs.map((p) => ({ name: PROV[p.key].short, v: p.avgLatency, key: p.key }));
  const tokData = provs.map((p) => ({ name: PROV[p.key].short, v: p.outTok, key: p.key }));
  return (
    <div className="space-y-5">
      <div className="text-sm" style={{ color: C.sub }}>
        Bundled synthetic sample: the same prompt set is shown across three providers to demonstrate comparison views. Connect live data before drawing provider conclusions.
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        {provs.map((p) => (
          <Panel key={p.key} className="p-5" style={{ borderColor: p.key === "anthropic" ? "rgba(224,134,83,0.4)" : C.border }}>
            <div className="flex items-center gap-2 mb-3">
              <Dot color={PROV[p.key].color} />
              <span className="font-semibold">{p.label}</span>
              {p.key === "anthropic" && <Pill color={C.red} bg="rgba(248,113,113,0.14)">regressed</Pill>}
            </div>
            <div className="grid grid-cols-2 gap-y-3 gap-x-2 text-sm">
              <Metric label="Pass rate" value={`${p.passRate}%`} color={p.passRate >= 95 ? C.green : C.red} />
              <Metric label="Avg latency" value={`${p.avgLatency}s`} />
              <Metric label="Error rate" value={`${p.errorRate}%`} color={p.errorRate > 5 ? C.amber : C.text} />
              <Metric label="Output tokens" value={k(p.outTok)} />
              <Metric label="Total cost" value={usd(p.cost)} color={C.green} />
              <Metric label="Traces" value={p.n.toLocaleString()} />
            </div>
          </Panel>
        ))}
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
          <li className="flex gap-2"><span style={{ color: C.blue }}>•</span> <span><span style={{ color: C.text }}>Treat bundled numbers as sample data</span>; your own traces are the only meaningful provider comparison.</span></li>
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
  const max = Math.max(...data.map((d) => d.v));
  return (
    <Panel className="p-5">
      <div className="font-semibold text-sm mb-3">{title}</div>
      <div className="space-y-2.5">
        {data.map((d) => (
          <div key={d.name}>
            <div className="flex items-center justify-between text-xs mb-1">
              <span className="flex items-center gap-1.5" style={{ color: C.sub }}><Dot color={PROV[d.key].color} />{d.name}</span>
              <span style={{ color: C.text, fontWeight: 600 }}>{fmt(d.v)}</span>
            </div>
            <div className="h-2 rounded-full overflow-hidden" style={{ background: C.grid }}>
              <div style={{ width: `${(d.v / max) * 100}%`, height: "100%", background: PROV[d.key].color }} />
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------- APP */
function App({ source = "sample", onReload, reloading }) {
  const [mode, setMode] = useState("landing");
  return (
    <div style={{ fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif", height: "100%", background: C.bg }}>
      <style>{"@keyframes vspin{to{transform:rotate(360deg)}}"}</style>
      {mode === "landing"
        ? <Landing onEnter={() => setMode("dashboard")} />
        : <Dashboard onExit={() => setMode("landing")} source={source} onReload={onReload} reloading={reloading} />}
    </div>
  );
}

// Root owns live-data loading. On mount it fetches API_URL; on success it swaps the
// module-level DATA to the live bundle and flips the indicator to "live". Any failure
// (e.g. in-chat preview with no server) silently keeps the embedded SEED snapshot.
export default function Root() {
  const [source, setSource] = useState("sample");
  const [reloading, setReloading] = useState(false);
  const [, setVersion] = useState(0);
  const load = React.useCallback(() => {
    setReloading(true);
    fetch(API_URL, { headers: { Accept: "application/json" } })
      .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then((d) => {
        if (d && d.meta && d.providers) { DATA = d; setSource("live"); setVersion((v) => v + 1); }
      })
      .catch(() => { /* no API reachable — keep SEED */ })
      .finally(() => setReloading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  return <App source={source} onReload={load} reloading={reloading} />;
}
