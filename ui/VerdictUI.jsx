import React, { useState, useEffect, useRef } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import {
  Activity, AlertTriangle, BarChart3, CheckCircle2, Layers, Scale, Search,
  Shield, Signal, Terminal, Gauge, Filter, X, Sparkles, Eye, RefreshCw, Info,
} from "lucide-react";
import { dimensionAxisLabel, dimensionLabel } from "./dimension-labels.js";
import { providerPresentation } from "./provider-presentation.js";
import { Operations } from "./Operations.jsx";
import { Registry } from "./Registry.jsx";
import { Runs } from "./Runs.jsx";
import { Usage } from "./Usage.jsx";
import { Insights } from "./Insights.jsx";
import { GuidedHome } from "./GuidedHome.jsx";
import { isEvidenceGap } from "./guided-flow.mjs";
import { IssueInbox } from "./IssueInbox.jsx";
import { EvidenceExplain } from "./EvidenceExplain.jsx";
import { EvaluatorLab } from "./EvaluatorLab.jsx";
import { InspectLab } from "./InspectLab.jsx";
import { ManagementReport } from "./ManagementReport.jsx";
import { ControlCenter } from "./ControlCenter.jsx";
import { Experiments } from "./Experiments.jsx";
import { SetupWizard } from "./SetupWizard.jsx";
import { initialDashboardTab } from "./source-state.mjs";
import { Monitor } from "./Monitor.jsx";
import { MatchedConversationCompare } from "./MatchedConversationCompare.jsx";
import {
  canonicalDashboardHash, parseDashboardRoute, parseDashboardSelection,
  serializeDashboardRoute,
} from "./dashboard-route.mjs";
import { formatLatency } from "./presentation-format.mjs";

// Embedded synthetic sample data. This keeps the static dashboard renderable when
// no live API is reachable. It is not a benchmark, experiment result, or claim
// about any provider.
const SEED = /* @__PURE__ */ (() => {
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
  const metric = (calls, failedCalls, inputTokens, outputTokens, costUsd, averageLatencyMs) => ({
    calls, errorRecordedCalls: failedCalls,
    endedNoErrorRecordedCalls: calls - failedCalls, unfinishedCalls: 0,
    inputTokens, outputTokens, totalTokens: inputTokens + outputTokens,
    tokenKnownCalls: calls, costUsd, costKnownCalls: calls,
    averageLatencyMs, latencyKnownCalls: calls,
  });
  return {
    meta: {
      runStart: 'sample-data', durationHours: 8, totalTraces: 96, monitorEligibleCalls: 96, totalJudged: 48,
      totalCost: 0.42, totalCostStatus: 'complete', regressionHour: null,
      providers: 3, clusters: 4, workload: 'sample-service',
    },
    managementReport: {
      schema: "management-report-v2",
      period: { days: 30, startDate: "2026-08-13", endDate: "2026-09-11" },
      scope: {
        firstCapturedAt: "2026-09-04T08:00:00+00:00", latestCapturedAt: "2026-09-11T20:00:00+00:00",
        ...metric(96, 1, 7230, 28200, 0.42, 2560),
        latencySampledCalls: 96, p50LatencyMs: 2480, p95LatencyMs: 3220,
        identifiedApplications: 2, unattributedCalls: 0, judgedCalls: 48, judgeErrorCalls: 0,
      },
      timeline: { availableDates: 8, shownDates: 8, rows:
        [6, 9, 12, 10, 15, 17, 11, 16].map((calls, index) => ({
          date: `2026-09-${String(index + 4).padStart(2, "0")}`, calls,
          totalTokens: calls * 369 + (index === 7 ? 6 : 0), tokenKnownCalls: calls,
        })),
      },
      applications: { availableRows: 2, shownRows: 2, rows: [
        { name: "support-assistant", environment: "production", attributed: true, ...metric(60, 1, 4500, 17600, 0.27, 2380) },
        { name: "engineering-copilot", environment: "production", attributed: true, ...metric(36, 0, 2730, 10600, 0.15, 2860) },
      ] },
      models: { availableRows: 3, shownRows: 3, rows: [
        { provider: "anthropic", model: "sample-model-a", ...metric(32, 0, 2400, 9600, 0.19, 2250) },
        { provider: "openai", model: "sample-model-b", ...metric(32, 0, 2380, 8200, 0.08, 2480) },
        { provider: "google", model: "sample-model-c", ...metric(32, 1, 2450, 10400, 0.15, 2960) },
      ] },
    },
    evaluation: {
      status: "selected", selectedId: "sample-evaluator",
      selectedIdentity: { id: "sample-evaluator", label: "sample-judge · sample-rubric v1", complete: true },
      availableIdentities: [{ id: "sample-evaluator", label: "sample-judge · sample-rubric v1", complete: true }],
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
    monitor: {
      active: {
        state: "active",
        policyState: "active",
        policy: {
          prospective_target: 12,
          grouping_mode: "none",
          evaluator_fingerprint: "sample-evaluator",
        },
        snapshot: {
          manifest: {
            reference_unit_ids: [
              "sample-reference-0", "sample-reference-1", "sample-reference-2", "sample-reference-3",
              "sample-reference-4", "sample-reference-5", "sample-reference-6", "sample-reference-7",
              "sample-reference-8", "sample-reference-9", "sample-reference-10", "sample-reference-11",
            ],
            current_unit_ids: [
              "sample-current-0", "sample-current-1", "sample-current-2", "sample-current-3",
              "sample-current-4", "sample-current-5", "sample-current-6", "sample-current-7",
              "sample-current-8", "sample-current-9", "sample-current-10", "sample-current-11",
            ],
            pending_evaluator_units: [],
            prospective_open: false,
            comparison_index: 1,
          },
          comparison: {
            status: "alert",
            alpha_threshold: 0.03,
            metrics: [{
              metric: "judge.completeness.pass",
              reference_value: 1,
              current_value: 0.5,
              effect: -0.5,
              p_adjusted: 0.008,
              reference_n: 12,
              current_n: 12,
              alert: true,
            }],
            metric_coverage: [],
            groups: [],
            unseen_group_share: 0,
            unassigned_group_share: 0,
          },
        },
      },
      candidate: null,
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
  meta: { totalTraces: 0, monitorEligibleCalls: 0, totalAgentRuns: 0, agentRunSources: [], agentRunSourcesTruncated: false, agentRunAgents: [], agentRunAgentsTruncated: false, lastAgentCaptureAt: null, totalJudged: 0, totalCost: null, providers: 0, clusters: 0, workload: null },
  evaluation: { status: "empty", selectedId: null, availableIdentities: [] },
  providers: [], clusters: [], dimensionOverall: [], tsRows: [],
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

function mountedRunsUrl() {
  return mountedApiUrl().replace(/\/api\/data(?:\?.*)?$/, "/api/runs");
}

function mountedUsageUrl() {
  return mountedApiUrl().replace(/\/api\/data(?:\?.*)?$/, "/api/usage");
}

function mountedInsightsUrl() {
  return mountedApiUrl().replace(/\/api\/data(?:\?.*)?$/, "/api/insights");
}

function useOperationsConfig() {
  const [operations, setOperations] = useState({
    operationsUrl: null,
    operationsPageUrl: null,
  });
  useEffect(() => {
    let active = true;
    fetch(mountedConfigUrl(), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => response.ok ? response.json() : null)
      .then((config) => {
        const adapter = config?.operationsUrl;
        const page = config?.operationsPageUrl;
        if (active) setOperations({
          operationsUrl: typeof adapter === "string" && adapter.startsWith("/") && !adapter.startsWith("//") ? adapter : null,
          operationsPageUrl: page === "/operations" ? page : null,
        });
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);
  return operations;
}

const API_URL = mountedApiUrl();

function apiUrlForEvaluator(evaluatorId, traceOffset = 0, traceJudgeStatus = "all", traceId = null, reportDays = 30, traceDate = null) {
  const params = new URLSearchParams();
  if (evaluatorId) params.set("evaluator", evaluatorId);
  if (traceOffset > 0) params.set("trace_offset", String(traceOffset));
  if (traceJudgeStatus !== "all") params.set("trace_judge_status", traceJudgeStatus);
  if (traceId) params.set("trace_id", traceId);
  if (traceDate) params.set("trace_date", traceDate);
  params.set("report_days", String(reportDays));
  return `${API_URL}?${params.toString()}`;
}

function useDashboardData() {
  const [state, setState] = useState({
    snapshot: EMPTY,
    source: "loading",
    loading: false,
    error: null,
    traceOffset: 0,
    traceJudgeStatus: "all",
    traceId: null,
    traceDate: null,
  });
  const requestSequence = useRef(0);
  const activeController = useRef(null);

  const load = React.useCallback(async (evaluatorId = null, traceOffset = 0, traceJudgeStatus = "all", traceId = null, reportDays = 30, traceDate = null) => {
    const requestId = requestSequence.current + 1;
    requestSequence.current = requestId;
    if (activeController.current) activeController.current.abort();
    const controller = new AbortController();
    activeController.current = controller;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(apiUrlForEvaluator(evaluatorId, traceOffset, traceJudgeStatus, traceId, reportDays, traceDate), {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) {
        const failure = await response.json().catch(() => null);
        if (response.status === 503 && failure?.state === "store_not_created") {
          // Setup has not created the local store yet: first run, not an error.
          if (requestId !== requestSequence.current) return false;
          setState((current) => ({ ...current, snapshot: EMPTY, source: "new", loading: false, error: null,
            traceOffset: 0, traceJudgeStatus: "all", traceId: null, traceDate: null }));
          return false;
        }
        throw new Error(`HTTP ${response.status}`);
      }
      const snapshot = await response.json();
      if (!snapshot || !snapshot.meta || !Array.isArray(snapshot.providers)) {
        throw new Error("invalid dashboard response");
      }
      if (requestId !== requestSequence.current) return false;
      setState({ snapshot, source: "live", loading: false, error: null, traceOffset, traceJudgeStatus, traceId, traceDate });
      return true;
    } catch (error) {
      if (requestId !== requestSequence.current) return false;
      if (error?.name === "AbortError") return false;
      setState((current) => {
        if (current.source !== "live") {
          return {
            ...current,
            loading: false,
            error: "Could not load dashboard data. Check the Verdict server log.",
          };
        }
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

  const reportDays = state.snapshot.managementReport?.period?.days ?? 30;
  return {
    ...state,
    load: (evaluatorId) => load(evaluatorId, 0, state.traceJudgeStatus, state.traceId, reportDays, state.traceDate),
    loadTracePage: (traceOffset) => load(state.snapshot.evaluation?.selectedId || null, traceOffset, state.traceJudgeStatus, state.traceId, reportDays, state.traceDate),
    loadTraceFilter: (traceJudgeStatus, traceId = null, traceDate = null) => {
      const selectedOnCurrentPage = traceId && traceJudgeStatus === state.traceJudgeStatus
        && traceDate === state.traceDate
        && state.snapshot.samples?.some((sample) => sample.trace_id === traceId);
      return load(state.snapshot.evaluation?.selectedId || null,
        selectedOnCurrentPage ? state.traceOffset : 0, traceJudgeStatus, traceId, reportDays, traceDate);
    },
    reload: (days = reportDays) => load(state.snapshot.evaluation?.selectedId || null, state.traceOffset, state.traceJudgeStatus, state.traceId, days, state.traceDate),
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
  if (elapsedSeconds >= 86400) {
    const days = Math.floor(elapsedSeconds / 86400);
    relative = `${days} ${days === 1 ? "day" : "days"} ago`;
  } else if (elapsedSeconds >= 3600) {
    const hours = Math.floor(elapsedSeconds / 3600);
    relative = `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  } else if (elapsedSeconds >= 60) {
    const minutes = Math.floor(elapsedSeconds / 60);
    relative = `${minutes} ${minutes === 1 ? "minute" : "minutes"} ago`;
  }
  return { absolute, relative };
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

/* ============================================================== DASHBOARD */
const TRUNCATION_LABELS = {
  providers: "providers",
  providerModels: "provider models",
  clusters: "clusters",
  dimensions: "dimensions",
  evaluatorIdentities: "evaluator identities",
  latencyPoints: "latency points",
  hourlyPoints: "quality points",
  traceSamples: "trace samples",
};
const TAB_HELP = {
  overview: "Start with recorded issues and token activity. Select a finding or day to inspect its evidence. Reliability, performance, and behavior details are available below.",
  explore: "Inspect Agent Runs and genuine LLM calls, including their evidence and evaluation state. Provider comparison is descriptive unless traffic is matched.",
  evaluate: "Configure evaluators, inspect stored judgments, analyze one-off JSON exports, and review labels. Missing evidence stays not evaluable rather than being scored as a failure.",
  monitor: "Review recorded issues, recurring checks, and worker state. Model-call drift and grouping have separate evidence and setup paths.",
  report: "A management-level view of utilization, reliability, quality evidence, and current Monitor state. Exports contain aggregates only.",
  settings: "Configure data sources, alert destinations, integrations, and privacy controls.",
};

const WORKSPACE_SECTIONS = {
  overview: [["summary", "Summary"], ["reliability", "Reliability"], ["performance", "Performance"], ["behavior", "Behavior"]],
  explore: [["runs", "Agent Runs & Tools"], ["calls", "LLM Calls"], ["compare", "Compare"]],
  evaluate: [["results", "Results"], ["lab", "Evaluator Lab"], ["cases", "Saved Cases"], ["inspect", "Inspect JSON"], ["review", "Review Queue"]],
  monitor: [["status", "Monitoring"], ["plan", "Suggested setup"], ["history", "Model drift setup"], ["segments", "Groups"], ["schedule", "Schedule"]],
  report: [["management", "Management Report"]],
  settings: [["sources", "Data Sources"], ["alerts", "Alerts"], ["integrations", "Integrations"], ["privacy", "Privacy"]],
};

function TabHelp({ tab, label, text }) {
  const [open, setOpen] = useState(false);
  const tooltipId = `tab-help-${tab}`;
  return <span className="relative inline-flex" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
    <button
      type="button"
      aria-label={`About ${label}`}
      aria-describedby={tooltipId}
      aria-expanded={open}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onClick={() => setOpen(true)}
      onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }}
      className="inline-flex p-1"
      style={{ color: open ? C.accent2 : C.faint }}
    >
      <Info size={15} />
    </button>
    {open && <span
      id={tooltipId}
      role="tooltip"
      className="absolute text-xs p-3"
      style={{
        top: "calc(100% + 6px)", left: 0, zIndex: 40,
        width: "min(320px, calc(100vw - 48px))", color: C.text,
        background: C.panel2, border: `1px solid ${C.border}`,
        borderRadius: 3, boxShadow: "0 12px 32px rgba(0,0,0,.45)",
      }}
    >{text}</span>}
  </span>;
}

function WorkspaceNav({ tab, section, onSection }) {
  if (WORKSPACE_SECTIONS[tab].length === 1 || (tab === "overview" && section === "summary")
    || (tab === "monitor" && ["plan", "status", "segments", "history"].includes(section))
    || (tab === "explore" && section === "runs")) return null;
  if (tab === "monitor") return <nav aria-label="monitor sections" className="mb-5 flex flex-wrap items-center gap-2 border-b pb-2" style={{ borderColor: C.border }}>
    {WORKSPACE_SECTIONS.monitor.slice(0, 2).map(([id, label]) => <button key={id} onClick={() => onSection(id)}
      className="px-4 py-3 text-sm border-b-2" style={{ color: section === id ? C.text : C.sub, borderColor: section === id ? C.accent : "transparent" }}>{label}</button>)}
    <details className="relative ml-auto text-sm" open={["history", "segments", "schedule"].includes(section) ? true : undefined}>
      <summary className="cursor-pointer px-3 py-2" style={{ color: C.sub }}>Advanced views</summary>
      <div className="flex flex-wrap gap-2 pb-2">{WORKSPACE_SECTIONS.monitor.slice(2).map(([id, label]) =>
        <button key={id} onClick={() => onSection(id)} className="border px-3 py-2" style={{ borderColor: C.border, color: section === id ? C.accent : C.sub }}>{label}</button>)}</div>
    </details>
  </nav>;
  return <nav aria-label={`${tab} sections`} className="mb-5 flex overflow-x-auto border-b" style={{ borderColor: C.border }}>
    {WORKSPACE_SECTIONS[tab].map(([id, label]) => <button key={id} onClick={() => onSection(id)}
      className="px-4 py-3 text-sm border-b-2 shrink-0"
      style={{ color: section === id ? C.text : C.sub, borderColor: section === id ? C.accent : "transparent" }}>
      {label}
    </button>)}
  </nav>;
}

function monitorAlertCount(monitor) {
  return (monitor?.active?.snapshot?.comparison?.metrics || [])
    .filter((metric) => metric.alert).length;
}

function navigateWorkflow(commitRoute, route, target) {
  if (WORKSPACE_SECTIONS[target?.tab]?.some(([section]) => section === target.section)) {
    return commitRoute({ ...route, tab: target.tab, section: target.section });
  }
  if (target?.tab === "evaluators") return commitRoute({ ...route, tab: "evaluate", section: "lab" });
  if (target?.tab === "drift" && target.section === "clusters") return commitRoute({ ...route, tab: "monitor", section: "segments" });
  return commitRoute({ ...route, tab: "monitor", section: target?.section === "explore" ? "history" : "status" });
}

function navigateSetup(commitRoute, route, destination) {
  const target = {
    insights: ["overview", "summary"], evaluators: ["evaluate", "lab"],
    explore: ["monitor", "history"], control: ["monitor", "schedule"],
  }[destination] || ["overview", "summary"];
  commitRoute({ ...route, tab: target[0], section: target[1] });
}

function Dashboard({ data = SEED, onExit, source = "sample", onReload, onEvaluatorChange, onTracePageChange, onTraceFilterChange, traceOffset = 0, loadedTraceFilter = { traceDate: null, traceJudgeStatus: "all", traceId: null }, reloading, loadError, operationsUrl = null, operationsPageUrl = null }) {
  const DATA = data;
  const initialSelection = useRef(parseDashboardSelection(
    typeof window === "undefined" ? "" : window.location.search,
  ));
  const initialRoute = useRef(parseDashboardRoute(
    typeof window === "undefined" ? "" : window.location.hash,
    initialDashboardTab(data.meta),
  ));
  const [route, setRoute] = useState(
    initialSelection.current.state === "valid"
      ? initialSelection.current.route : initialRoute.current,
  );
  const [evaluationSelection, setEvaluationSelection] = useState(null);
  const [caseDraft, setCaseDraft] = useState(null);
  const [selectionError, setSelectionError] = useState(
    initialSelection.current.state === "invalid",
  );
  const tab = route.tab;
  const commitRoute = React.useCallback((next, replace = false) => {
    const normalized = parseDashboardRoute(serializeDashboardRoute(next), "overview");
    setRoute(normalized);
    setSelectionError(false);
    if (typeof window !== "undefined") {
      const destination = window.location.search
        ? `${window.location.pathname}${serializeDashboardRoute(normalized)}`
        : serializeDashboardRoute(normalized);
      window.history[replace ? "replaceState" : "pushState"](
        {}, "", destination,
      );
    }
  }, []);
  const setTab = React.useCallback((nextTab) => {
    commitRoute({
      ...route, tab: nextTab, findingCode: null, runIds: [],
      selectedRunId: null, runIdsTruncated: false,
    });
  }, [commitRoute, route]);
  const openFindingRun = React.useCallback((finding) => {
    const runIds = Array.isArray(finding?.runIds) ? finding.runIds : [];
    commitRoute({ ...route,
      tab: "explore", section: "runs", findingCode: finding?.code || null, runIds,
      selectedRunId: runIds[0] || null,
      runIdsTruncated: finding?.runIdsTruncated === true,
    });
  }, [commitRoute, route]);
  useEffect(() => {
    const restore = () => {
      const fallback = initialDashboardTab(DATA.meta);
      const selection = parseDashboardSelection(window.location.search);
      if (selection.state === "valid") {
        setRoute(selection.route);
        setSelectionError(false);
        window.history.replaceState(
          {}, "", `${window.location.pathname}${serializeDashboardRoute(selection.route)}`,
        );
        return;
      }
      if (selection.state === "invalid") {
        setRoute(parseDashboardRoute("", fallback));
        setSelectionError(true);
        return;
      }
      const canonical = canonicalDashboardHash(window.location.hash, fallback);
      setRoute(parseDashboardRoute(canonical || window.location.hash, fallback));
      if (canonical) window.history.replaceState({}, "", canonical);
    };
    restore();
    window.addEventListener("popstate", restore);
    window.addEventListener("hashchange", restore);
    return () => { window.removeEventListener("popstate", restore); window.removeEventListener("hashchange", restore); };
  }, [DATA.meta]);
  const traceFilterCallback = useRef(onTraceFilterChange);
  useEffect(() => { traceFilterCallback.current = onTraceFilterChange; }, [onTraceFilterChange]);
  useEffect(() => {
    if (source === "live" && route.tab === "explore" && route.section === "calls" && traceFilterCallback.current) {
      traceFilterCallback.current(route.traceJudgeStatus, route.traceId, route.traceDate || null);
    }
  }, [route.section, route.tab, route.traceId, route.traceJudgeStatus, route.traceDate, source]);
  const initialRouteResolved = useRef(
    source !== "loading" || initialRoute.current.explicit
      || initialSelection.current.state === "valid",
  );
  useEffect(() => {
    if (source === "live" && !initialRouteResolved.current) {
      initialRouteResolved.current = true;
      setTab(initialDashboardTab(DATA.meta));
    }
  }, [source, DATA.meta]);
  const evaluation = DATA.evaluation || { status: "empty", selectedId: null, availableIdentities: [] };
  const evaluatorRelevant = (tab === "evaluate" && route.section === "results")
    || (tab === "explore" && route.section === "calls");
  const evaluatorCallback = useRef(onEvaluatorChange);
  useEffect(() => { evaluatorCallback.current = onEvaluatorChange; }, [onEvaluatorChange]);
  useEffect(() => {
    if (source === "live" && evaluatorRelevant && route.evaluatorId && evaluation.selectedId !== route.evaluatorId) {
      evaluatorCallback.current?.(route.evaluatorId);
    }
  }, [evaluation.selectedId, evaluatorRelevant, route.evaluatorId, source]);
  const evaluatorSelectionNeeded = ["selection_required", "invalid_selection"].includes(evaluation.status);
  const traceFilterMatches = (route.traceDate || null) === (loadedTraceFilter.traceDate || null)
    && route.traceJudgeStatus === loadedTraceFilter.traceJudgeStatus
    && (route.traceId || null) === (loadedTraceFilter.traceId || null);
  const boundedResources = Object.entries(DATA.truncation?.resources || {})
    .filter(([name, resource]) => (source !== "live" || name !== "traceSamples") && resource.shown < resource.available);
  const summaryOpen = tab === "overview" && route.section === "summary";
  const truncationNotice = DATA.truncation?.applied && boundedResources.length > 0 && (
    <div role="status" className="px-3 py-3 border flex items-start gap-2 text-sm"
      style={{ borderColor: "#6b5529", background: C.amberBg, color: C.text, borderRadius: 3 }}>
      <AlertTriangle size={16} style={{ color: C.amber, marginTop: 1, flexShrink: 0 }} />
      <span>Some dashboard views are bounded. Store totals remain complete; shown versus available: {boundedResources.map(([name, resource]) => (
        `${TRUNCATION_LABELS[name] || name}: ${resource.shown.toLocaleString()} of ${resource.available.toLocaleString()}`
      )).join("; ")}.</span>
    </div>
  );
  const syntheticStore = source === "live" && DATA.meta.dataProvenance === "synthetic";
  const sourceLabel = syntheticStore ? "Synthetic demo" : source === "live" ? "Connected store" : source === "sample" ? "Synthetic sample" : source === "new" ? "No store yet" : loadError && !reloading ? "Evidence unavailable" : "Checking evidence";
  const workloadLabel = syntheticStore ? "Synthetic local demo" : source === "sample"
    ? "Sample workload"
    : source === "loading" ? "Verdict workspace"
    : source === "new" ? "New workspace"
    : DATA.meta.workload
      ? `Workload: ${DATA.meta.workload}`
      : "Verdict store";
  const primaryNav = [
    { tab: "overview", section: "summary", label: "Home", icon: Gauge },
    { tab: "monitor", section: "plan", label: "Suggested setup", icon: Sparkles },
    { tab: "monitor", section: "status", label: "Monitoring", icon: Signal, badge: monitorAlertCount(DATA.monitor) },
    { tab: "explore", section: "runs", label: "Investigation", icon: Search },
    { tab: "monitor", section: "segments", label: "Groups", icon: Layers },
    { tab: "monitor", section: "history", label: "Model drift setup", icon: Activity },
  ];
  const secondaryNav = [
    { tab: "evaluate", section: "results", label: "Evaluate", icon: Scale },
    { tab: "report", section: "management", label: "Report", icon: BarChart3 },
    { tab: "settings", section: "sources", label: "Settings", icon: Terminal },
  ];
  const focusedIssueHeading = {
    test_failed: "A captured run recorded a failed test",
    command_failed: "A captured command failed",
    tool_error: "A tool action reported an error",
    business_outcome_failed: "A reported outcome failed",
    possible_tool_loop: "Repeated tool calls need review",
  }[route.findingCode] || "Inspect the recorded finding";
  const focusedGap = isEvidenceGap({ code: route.findingCode });
  const pageIntro = {
    "overview/summary": ["Home", "Know what needs attention", "Verdict summarizes captured evidence and points to the next useful action."],
    "monitor/plan": ["Suggested setup", "A monitoring plan for this workspace", "Review captured evidence and the checks that will run before you enable recurring monitoring."],
    "monitor/status": ["Monitoring", "What Verdict watches", "Scheduled agent checks, evidence freshness, and eligible model-call changes in one place."],
    "explore/runs": route.findingCode
      ? ["Investigation", focusedGap ? "Inspect an evidence gap" : focusedIssueHeading,
        focusedGap ? "Review what was and was not recorded before drawing conclusions." : "Review the recorded finding, its linked evidence, and the next check."]
      : route.selectedRunId || route.eventId
      ? ["Investigation", "Inspect agent evidence", "Review what happened in this run, its recorded evidence, and the next check."]
      : ["Investigation", "Investigate recorded evidence", "Open a run, turn, or event to see what happened and what to check next."],
    "monitor/segments": ["Groups", "Find where a problem is concentrated",
      Number(DATA.meta.totalTraces || 0) > 0
        ? "Start with recorded source and model fields; add semantic groups only when the evidence supports them."
        : "Start with recorded sources and agent names. Provider/model groups need captured model calls; semantic groups need usable prompt evidence."],
    "monitor/history": ["Model drift setup", "Set up a model-call alert", "Review genuine provider calls, the reference cohort, and sample coverage before activation."],
  }[`${tab}/${route.section}`] || [tab === "report" ? "Report" : tab === "settings" ? "Settings" : tab === "explore" ? "Investigation" : tab === "overview" ? "Home" : tab === "monitor" ? "Monitoring" : "Evaluate", WORKSPACE_SECTIONS[tab]?.find(([section]) => section === route.section)?.[1] || "Verdict", null];
  const workspaceLabel = pageIntro[1];
  const activeNav = (item) => tab === item.tab && (route.section === item.section
    || ["overview", "explore", "evaluate", "report", "settings"].includes(item.tab)
    || (item.tab === "monitor" && item.section === "plan" && route.section === "schedule"));
  const navigateSidebar = (item) => commitRoute({ ...route, tab: item.tab, section: item.section,
    findingCode: null, runIds: [], selectedRunId: null, runIdsTruncated: false,
    eventId: null, traceId: null, evaluatorId: null, sourceKind: null, agentName: null });
  const openUsageRun = (runId, eventId = null, recentRevision = null, recentTurnId = null) =>
    commitRoute({ ...route, tab: "explore", section: "runs", findingCode: null,
      runIds: [runId], selectedRunId: runId, runIdsTruncated: false,
      eventId: recentRevision ? null : eventId,
      recentRevision: recentRevision || null, recentTurnId: recentTurnId || null,
      recentEventId: recentRevision ? eventId : null, noticeId: null });
  return (
    <div className="verdict-workspace-shell" style={{ background: "#0b100f", color: C.text }}>
      <aside className="verdict-workspace-sidebar">
        <button type="button" onClick={onExit} title="Back to home" className="verdict-workspace-brand"><Logo size={27} /><span>Verdict</span></button>
        <div className="verdict-workspace-context"><strong>{workloadLabel}</strong><span>{sourceLabel}</span><small>{source === "loading" ? "Captured evidence not loaded yet" : source === "new" ? "Create a local store to begin" : <>{Number(DATA.meta.totalAgentRuns || 0).toLocaleString()} recorded agent runs · {Number(DATA.meta.totalTraces || 0).toLocaleString()} model calls</>}</small></div>
        <div className="verdict-nav-label">WORKSPACE</div>
        <nav aria-label="Workspace" className="verdict-workspace-nav">
          {primaryNav.map((item) => <button key={item.label} type="button" onClick={() => navigateSidebar(item)} aria-current={activeNav(item) ? "page" : undefined} className={`verdict-nav-item ${activeNav(item) ? "active" : ""}`}><item.icon size={17} /><span>{item.label}</span>{item.badge ? <b>{item.badge}</b> : null}</button>)}
        </nav>
        <div className="verdict-nav-label">MORE</div>
        <nav aria-label="More" className="verdict-workspace-nav">
          {secondaryNav.map((item) => <button key={item.label} type="button" onClick={() => navigateSidebar(item)} aria-current={activeNav(item) ? "page" : undefined} className={`verdict-nav-item ${activeNav(item) ? "active" : ""}`}><item.icon size={17} /><span>{item.label}</span></button>)}
          {operationsPageUrl === "/operations" && <button type="button" onClick={() => window.location.assign(operationsPageUrl)} className="verdict-nav-item"><Activity size={17} /><span>Operations</span></button>}
        </nav>
      </aside>
      <div className="verdict-workspace-body">
        <header className="verdict-workspace-topbar"><span>{workloadLabel} / Agent reliability</span><div className="verdict-topbar-actions">{(source === "sample" || syntheticStore) && <span className="verdict-synthetic-tag">ILLUSTRATIVE SYNTHETIC DATA</span>}<button onClick={() => onReload()} disabled={reloading} title="Refresh from API" aria-label="Refresh from API" className="verdict-refresh-button"><RefreshCw size={15} style={{ animation: reloading ? "vspin 0.8s linear infinite" : "none" }} /></button></div></header>

      <main className="verdict-workspace-content overflow-x-hidden">
        {tab === "explore" && route.section === "runs" && (route.selectedRunId || route.eventId) && <button type="button" className="verdict-back-link" onClick={() => commitRoute({ ...route, tab: "monitor", section: "status", findingCode: null, runIds: [], selectedRunId: null, eventId: null })}>View monitoring</button>}
        <div className="verdict-page-intro">
          <div className="verdict-page-eyebrow">{pageIntro[0]}</div>
          <div className="flex items-center gap-2 mt-1"><h1>{workspaceLabel}</h1>{TAB_HELP[tab] && <TabHelp tab={tab} label={workspaceLabel} text={TAB_HELP[tab]} />}</div>
          {pageIntro[2] && <p>{pageIntro[2]}</p>}
        </div>
        <WorkspaceNav tab={tab} section={route.section} onSection={(section) => commitRoute({ ...route, section })} />
        {tab === "explore" && route.section === "runs" && !route.findingCode && <details className="verdict-secondary-path"><summary>Other investigation views</summary><div><button type="button" onClick={() => commitRoute({ ...route, section: "calls", runIds: [], selectedRunId: null, eventId: null, traceId: null })}>Browse model calls</button><button type="button" onClick={() => commitRoute({ ...route, section: "compare", runIds: [], selectedRunId: null, eventId: null })}>Compare recorded conversations</button></div></details>}
        {evaluatorRelevant && evaluation.availableIdentities.length > 0 && (
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
              onChange={(event) => { const evaluatorId = event.target.value; commitRoute({ ...route, evaluatorId }); onEvaluatorChange(evaluatorId); }}
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
        {selectionError && (
          <div role="alert" className="mb-5 px-3 py-3 border flex items-start gap-2 text-sm"
            style={{ borderColor: "#6b5529", background: C.amberBg, color: C.text, borderRadius: 3 }}>
            <AlertTriangle size={16} style={{ color: C.amber, marginTop: 1, flexShrink: 0 }} />
            <span>The requested Verdict link is invalid. No trace or agent activity was selected.</span>
          </div>
        )}
        {!summaryOpen && tab !== "monitor" && truncationNotice && <div className="mb-5">{truncationNotice}</div>}
        {tab === "overview" && route.section === "summary" && <>
          <GuidedHome meta={DATA.meta} monitor={DATA.monitor} managementReport={DATA.managementReport} source={source}
            insightsUrl={mountedInsightsUrl()} usageUrl={mountedUsageUrl()} configUrl={mountedConfigUrl()}
            onReload={onReload} onOpenFinding={openFindingRun}
            onOpenRun={openUsageRun}
            onNavigate={(nextTab, section, traceDate = null) => commitRoute({ ...route, tab: nextTab, section, traceDate, traceId: null })} />
          {truncationNotice}
        </>}
        {tab === "overview" && route.section === "reliability" && <Insights url={mountedInsightsUrl()} onOpenRuns={openFindingRun} onAnalysisComplete={onReload} mode="reliability" />}
        {tab === "overview" && route.section === "performance" && <div className="space-y-5">
          {source === "live" && <Usage url={mountedUsageUrl()} onOpenRun={openUsageRun} />}
          {source === "live" ? <details className="border p-5" style={{ borderColor: C.border, background: C.panel }}>
            <summary className="cursor-pointer font-semibold">Model-call diagnostics</summary>
            <div className="mt-5"><Insights url={mountedInsightsUrl()} onOpenRuns={openFindingRun} onAnalysisComplete={onReload} mode="performance" /></div>
          </details> : <Insights url={mountedInsightsUrl()} onOpenRuns={openFindingRun} onAnalysisComplete={onReload} mode="performance" />}
        </div>}
        {tab === "overview" && route.section === "behavior" && <Insights url={mountedInsightsUrl()} onOpenRuns={openFindingRun} onAnalysisComplete={onReload} mode="behavior" />}
        {tab === "explore" && route.section === "runs" && <Runs
          url={mountedRunsUrl()}
          focusRunIds={route.runIds}
          selectedRunId={route.selectedRunId}
          findingCode={route.findingCode}
          runIdsTruncated={route.runIdsTruncated}
          sourceKind={route.runIds.length ? null : route.sourceKind || null}
          agentName={route.runIds.length ? null : route.agentName || null}
          onClearGroup={() => commitRoute({ ...route, sourceKind: null, agentName: null, selectedRunId: null, eventId: null })}
          evaluatorFingerprint={evaluation.selectedIdentity?.fingerprint || null}
          routedEventId={route.eventId || null}
          recentRevision={route.recentRevision || null}
          recentTurnId={route.recentTurnId || null}
          recentEventId={route.recentEventId || null}
          onOpenRecentEvent={({ runId, revision, turnId, eventId }) => commitRoute({
            ...route, runIds: [runId], selectedRunId: runId, recentRevision: revision,
            recentTurnId: turnId, recentEventId: eventId,
          })}
          noticeId={route.noticeId || null}
          onOpenNoticeEvidence={(runId, eventId, recentRevision, recentTurnId) => commitRoute({ ...route, tab: "explore", section: "runs", findingCode: null, runIds: [runId], selectedRunId: runId, runIdsTruncated: false, eventId: recentRevision ? null : eventId, recentRevision: recentRevision || null, recentTurnId: recentTurnId || null, recentEventId: recentRevision ? eventId : null })}
          onSelectRun={(runId) => commitRoute({ ...route, selectedRunId: runId,
            noticeId: null, recentRevision: null, recentTurnId: null, recentEventId: null })}
          onOpenRelatedRun={(runId) => commitRoute({ ...route, tab: "explore", section: "runs", findingCode: null, runIds: [runId], selectedRunId: runId, runIdsTruncated: false, eventId: null, noticeId: null })}
          onBrowseRecordedGroup={({ agentName, sourceKind }) => commitRoute({
            ...route, tab: "explore", section: "runs", findingCode: null,
            runIds: [], selectedRunId: null, runIdsTruncated: false, eventId: null,
            agentName, sourceKind,
          })}
          onShowAll={() => commitRoute({ ...route, tab: "explore", section: "runs", findingCode: null, runIds: [], selectedRunId: null, sourceKind: null, agentName: null })}
          onOpenTrace={(traceId) => commitRoute({ ...route, tab: "explore", section: "calls", traceJudgeStatus: "all", traceId })}
          onEvaluateTurn={(runId, turnId) => { setEvaluationSelection({ unit: "agent_turn", runId, turnId }); commitRoute({ ...route, tab: "evaluate", section: "lab" }); }}
          onDraftCase={(draft) => { setCaseDraft(draft); commitRoute({ ...route, tab: "evaluate", section: "cases" }); }}
        />}
        {tab === "explore" && route.section === "calls" && source === "new"
          && <Panel className="p-5"><p role="status" className="text-sm" style={{ color: C.sub }}>
            No local store is available. Start capture in Settings, then return to investigate model calls.
          </p></Panel>}
        {tab === "explore" && route.section === "calls" && source === "loading"
          && <Panel className="p-5"><p role="status" className="text-sm" style={{ color: C.sub }}>
            Loading model calls…
          </p></Panel>}
        {tab === "explore" && route.section === "calls" && source === "live" && !traceFilterMatches
          && <Panel className="p-5"><p role={loadError && !reloading ? "alert" : "status"} className="text-sm" style={{ color: loadError && !reloading ? C.amber : C.sub }}>
            {loadError && !reloading ? "Could not load calls for the selected filter. The previous result is hidden." : "Loading calls for the selected filter…"}
          </p>{loadError && !reloading && <button type="button" className="mt-3 text-sm underline" style={{ color: C.accent }}
            onClick={() => onTraceFilterChange?.(route.traceJudgeStatus, route.traceId, route.traceDate || null)}>Retry loading calls</button>}</Panel>}
        {tab === "explore" && route.section === "calls" && (source === "sample" || (source === "live" && traceFilterMatches)) && <Traces key={evaluation.selectedId || "none"} data={DATA} source={source}
          traceOffset={traceOffset} reloading={reloading} onTracePageChange={onTracePageChange}
          judgeStatus={route.traceJudgeStatus} selectedTraceId={route.traceId} traceDate={route.traceDate || null}
          onClearDate={() => commitRoute({ ...route, traceDate: null, traceId: null })}
          onJudgeStatus={(traceJudgeStatus) => commitRoute({
            ...route, tab: "explore", section: "calls", traceJudgeStatus, traceId: null,
          })}
          onSelectTrace={(traceId) => commitRoute({ ...route, tab: "explore", section: "calls", traceId })}
          onEvaluateTrace={(traceId) => {
            setEvaluationSelection({ unit: "trace", traceId });
            commitRoute({ ...route, tab: "evaluate", section: "lab" });
          }} />}
        {tab === "explore" && route.section === "compare" && <><MatchedConversationCompare configUrl={mountedConfigUrl()} source={source} /><Compare data={DATA} source={source} /></>}
        {tab === "evaluate" && route.section === "results" && <Judge data={DATA} onOpenOperations={operationsUrl ? () => commitRoute({ ...route, tab: "settings", section: "integrations" }) : null} />}
        {tab === "evaluate" && route.section === "lab" && <EvaluatorLab configUrl={mountedConfigUrl()} selection={evaluationSelection} onClearSelection={() => setEvaluationSelection(null)} onOpenEvaluated={(evaluatorId) => { onEvaluatorChange?.(evaluatorId); commitRoute({ ...route, tab: "explore", section: "calls", evaluatorId, traceJudgeStatus: "judged", traceId: null }); }} />}
        {tab === "evaluate" && route.section === "cases" && <Experiments root={mountedConfigUrl().replace(/\/api\/config$/, "")} draft={caseDraft} />}
        {tab === "evaluate" && route.section === "inspect" && <InspectLab configUrl={mountedConfigUrl()} />}
        {tab === "evaluate" && route.section === "review" && <ControlCenter section="review" configUrl={mountedConfigUrl()} onNavigate={(target) => navigateWorkflow(commitRoute, route, target)} />}
        {tab === "monitor" && source === "loading" && <Panel className="p-5"><p role={loadError && !reloading ? "alert" : "status"} className="text-sm" style={{ color: loadError && !reloading ? C.amber : C.sub }}>{loadError && !reloading ? "Could not load this workspace's evidence. Monitoring and setup are unavailable until it loads." : "Checking captured evidence and monitoring setup…"}</p>{loadError && !reloading && <button type="button" className="mt-3 text-sm underline" style={{ color: C.accent }} onClick={() => onReload?.()}>Retry loading evidence</button>}</Panel>}
        {tab === "monitor" && source !== "loading" && route.section === "status" && <IssueInbox configUrl={mountedConfigUrl()} insightsUrl={mountedInsightsUrl()} meta={DATA.meta} monitor={DATA.monitor} onRefreshStore={onReload} onOpenFinding={openFindingRun} onOpenPlan={() => commitRoute({ ...route, tab: "monitor", section: "plan", evaluatorId: null })} onOpenUsage={() => commitRoute({ ...route, tab: "overview", section: "summary", evaluatorId: null })} onOpenHistory={() => commitRoute({ ...route, tab: "monitor", section: "history", evaluatorId: null })} onOpenGroups={() => commitRoute({ ...route, tab: "monitor", section: "segments", evaluatorId: null })} onOpenDeliveries={() => commitRoute({ ...route, tab: "settings", section: "alerts", evaluatorId: null })} onOpenEvidence={(issue) => commitRoute({ ...route, tab: "explore", section: "runs", findingCode: null, runIds: [issue.runId], selectedRunId: issue.runId, runIdsTruncated: false, eventId: issue.recentRevision ? null : issue.eventId, recentRevision: issue.recentRevision || null, recentTurnId: issue.recentTurnId || null, recentEventId: issue.recentRevision ? issue.eventId : null, noticeId: issue.noticeId || null, evaluatorId: null })} />}
        {tab === "monitor" && source !== "loading" && route.section === "history" && <Monitor view="history" initialState={DATA.monitor} configUrl={mountedConfigUrl()} evaluation={data.evaluation} traceCount={DATA.meta.monitorEligibleCalls || 0} totalTraces={DATA.meta.totalTraces} agentRunCount={DATA.meta.totalAgentRuns} onSetup={() => commitRoute({ ...route, tab: "settings", section: "sources" })} onPlan={() => commitRoute({ ...route, tab: "monitor", section: "plan" })} onGroups={() => commitRoute({ ...route, tab: "monitor", section: "segments" })} onChanged={onReload} onOpenTrace={(traceId, evaluatorId) => commitRoute({ ...route, tab: "explore", section: "calls", traceId, evaluatorId })} />}
        {tab === "monitor" && source !== "loading" && route.section === "segments" && <Registry url={mountedRegistryUrl()} operationsUrl={operationsUrl} configUrl={mountedConfigUrl()} traceCount={DATA.meta.monitorEligibleCalls || 0} sourceGroups={DATA.meta.agentRunSources || []} sourceGroupsTruncated={DATA.meta.agentRunSourcesTruncated} agentGroups={DATA.meta.agentRunAgents || []} agentGroupsTruncated={DATA.meta.agentRunAgentsTruncated} modelRows={DATA.managementReport?.models?.rows || []} modelRowsAvailable={DATA.managementReport?.models?.availableRows || 0} onSourceGroup={(sourceKind) => commitRoute({ ...route, tab: "explore", section: "runs", sourceKind, agentName: null, runIds: [], selectedRunId: null, eventId: null })} onAgentGroup={(agentName) => commitRoute({ ...route, tab: "explore", section: "runs", agentName, sourceKind: null, runIds: [], selectedRunId: null, eventId: null })} onRuns={() => commitRoute({ ...route, tab: "explore", section: "runs", sourceKind: null, agentName: null, runIds: [], selectedRunId: null, eventId: null })} onOpenRun={(runId) => commitRoute({ ...route, tab: "explore", section: "runs", sourceKind: null, agentName: null, runIds: [runId], selectedRunId: runId, eventId: null })} onPlan={() => commitRoute({ ...route, tab: "monitor", section: "plan" })} onMonitor={() => commitRoute({ ...route, tab: "monitor", section: "history" })} onSetup={() => commitRoute({ ...route, tab: "settings", section: "sources" })} />}
        {tab === "monitor" && source !== "loading" && ["plan", "schedule"].includes(route.section) && <ControlCenter section="schedule" embedded={route.section === "plan"} configUrl={mountedConfigUrl()} meta={DATA.meta} monitor={DATA.monitor} onNavigate={(target) => navigateWorkflow(commitRoute, route, target)} />}
        {tab === "report" && route.section === "management" && <ManagementReport data={DATA} source={source} onPeriodChange={onReload} />}
        {tab === "settings" && route.section === "sources" && <SetupWizard configUrl={mountedConfigUrl()} agentSummary={DATA.meta} onRefresh={onReload} onNavigate={(destination) => navigateSetup(commitRoute, route, destination)} onComplete={() => { onReload(); commitRoute({ ...route, tab: "overview", section: "summary" }); }} />}
        {tab === "settings" && route.section === "alerts" && <ControlCenter section="alerts" configUrl={mountedConfigUrl()} />}
        {tab === "settings" && route.section === "privacy" && <ControlCenter section="privacy" configUrl={mountedConfigUrl()} />}
        {tab === "settings" && route.section === "integrations" && (operationsUrl ? <Operations url={operationsUrl} costBreakdown={DATA.meta.costBreakdown} /> : <Panel className="p-5"><div className="font-semibold">No operations adapter configured</div><div className="text-sm mt-2" style={{ color: C.sub }}>Data ingestion and evaluation remain available. Configure an authenticated operations adapter to expose infrastructure metrics and backend jobs here.</div></Panel>)}
      </main>
      </div>
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

/* ---------------------------------------------------------------- TRACES */
function Traces({ data = SEED, source = "sample", traceOffset = 0, reloading = false, onTracePageChange = null, judgeStatus = "all", selectedTraceId: routedTraceId = null, traceDate = null, onClearDate = null, onJudgeStatus = null, onSelectTrace = null, onEvaluateTrace = null }) {
  const DATA = data;
  const [prov, setProv] = useState("all");
  const [capture, setCapture] = useState("all");
  const [q, setQ] = useState("");
  const [selectedTraceId, setSelectedTraceId] = useState(routedTraceId);
  useEffect(() => setSelectedTraceId(routedTraceId), [routedTraceId]);
  const sel = DATA.samples.find((sample) => sample.trace_id === selectedTraceId)
    || (DATA.selectedTrace?.trace_id === selectedTraceId ? DATA.selectedTrace : null);
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
  const pageSize = traceResource?.limit ?? 30;
  const pageStart = shown ? traceOffset + 1 : 0;
  const pageEnd = shown ? traceOffset + shown : 0;

  return (
    <div className="flex flex-col xl:flex-row gap-5">
      <div className="flex-1 min-w-0">
        {traceDate && <div className="mb-3 text-sm flex flex-wrap items-center gap-3" role="status">
          <span>Model calls on {traceDate} UTC</span>
          <button type="button" className="underline" style={{ color: C.accent }} onClick={onClearDate}>Show all dates</button>
        </div>}
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
          <select aria-label="Evaluation status" value={judgeStatus} onChange={(event) => onJudgeStatus?.(event.target.value)} className="border px-3 py-2 text-xs" style={{ borderColor: C.border, background: C.panel }}>
            <option value="all">All evaluation states</option><option value="judged">Evaluated only</option><option value="not_judged">Not evaluated</option><option value="judge_error">Judge error</option><option value="pass">Pass</option><option value="fail">Fail</option><option value="unclear">Unclear</option>
          </select>
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
          <div style={{ minWidth: 1020 }}>
          <div className="grid text-xs px-4 py-2.5 border-b" style={{ borderColor: C.border, color: C.sub, gridTemplateColumns: "150px 1fr 88px 96px 70px 110px 110px" }}>
            <span>Recorded</span><span>Prompt</span><span>Cluster</span><span>Tokens</span><span>Latency</span><span>Execution</span><span>Evaluation</span>
          </div>
          <div style={{ maxHeight: 520, overflowY: "auto" }}>
            {rows.map((s) => {
              const on = sel && sel.trace_id === s.trace_id;
              const judgmentStatus = s.judgeStatus || "not_judged";
              const provider = providerPresentation(s.provider, s.request_model);
              const recorded = traceTime(s);
              return (
                <button key={s.trace_id} onClick={() => { setSelectedTraceId(s.trace_id); onSelectTrace?.(s.trace_id); }}
                  className="w-full grid items-center text-left px-4 py-2.5 border-b text-sm"
                  style={{ borderColor: C.grid, gridTemplateColumns: "150px 1fr 88px 96px 70px 110px 110px", background: on ? C.raised : "transparent" }}>
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
                  <span><Pill color={C.sub}>{s.cluster_label || s.cluster_id || "Unassigned"}</Pill></span>
                  <span className="text-xs" style={{ color: C.sub }}>{s.input_tokens ?? "—"}/{s.output_tokens ?? "—"}</span>
                  <span className="text-xs" style={{ color: C.sub }}>{s.latency_ms ? `${(s.latency_ms / 1000).toFixed(1)}s` : "—"}</span>
                  <span>{s.traceState === "unfinished" ? <Pill color={C.amber} bg={C.amberBg}>Unfinished</Pill> : s.providerStatus === "provider_error" ? <Pill color={C.red} bg={C.redBg}>provider error</Pill> : <Pill color={C.faint}>No provider error recorded</Pill>}</span>
                  <span>{judgmentStatus === "judge_error" ? <Pill color={C.red} bg={C.redBg}>judge error</Pill> : judgmentStatus === "fail" ? <Pill color={C.red} bg={C.redBg}>fail</Pill> : judgmentStatus === "unclear" ? <Pill color={C.amber} bg={C.amberBg}>unclear</Pill> : judgmentStatus === "pass" ? <Pill color={C.green} bg={C.greenBg}>pass</Pill> : <Pill color={C.faint}>not evaluated</Pill>}</span>
                </button>
              );
            })}
            {rows.length === 0 && <div className="p-8 text-center text-sm" style={{ color: C.faint }}>No traces match.</div>}
          </div>
          </div>
        </Panel>
        {source === "live" ? (
          <div className="text-xs mt-2 flex flex-wrap items-center gap-2" style={{ color: C.faint }}>
            <span className="flex-1">{traceDate
              ? `Showing ${pageStart.toLocaleString()}–${pageEnd.toLocaleString()} of ${available.toLocaleString()} model calls on this UTC date. Judge and replay traffic are excluded. Date filters before paging; other filters apply to this page.`
              : `Showing ${pageStart.toLocaleString()}–${pageEnd.toLocaleString()} of ${available.toLocaleString()} application traces. Judge telemetry remains excluded. Filters apply to this page.`}</span>
            <button onClick={() => onTracePageChange(Math.max(0, traceOffset - pageSize))}
              disabled={reloading || traceOffset === 0 || !onTracePageChange}
              className="px-2.5 py-1 border" style={{ borderColor: C.border, borderRadius: 3 }}>Previous</button>
            <button onClick={() => onTracePageChange(traceOffset + pageSize)}
              disabled={reloading || traceOffset + shown >= available || !onTracePageChange}
              className="px-2.5 py-1 border" style={{ borderColor: C.border, borderRadius: 3 }}>Next</button>
          </div>
        ) : (
          <div className="text-xs mt-2" style={{ color: C.faint }}>
            {`Showing newest ${shown.toLocaleString()} of ${available.toLocaleString()} application traces. Judge telemetry remains in store totals and is excluded here. Filters apply to this bounded view. ${rows.length.toLocaleString()} match the current filters.`}
          </div>
        )}
      </div>

      {/* detail */}
      <div className="shrink-0" style={{ width: "min(100%, 360px)" }}>
        {sel ? <TraceDetail s={sel} source={source} onEvaluate={onEvaluateTrace ? () => onEvaluateTrace(sel.trace_id) : null} onClose={() => {
          setSelectedTraceId(null);
          onSelectTrace?.(null);
        }} /> : (
          <Panel className="p-8 text-center">
            <Eye size={22} style={{ color: C.faint, margin: "0 auto" }} />
            <div className="text-sm mt-2" style={{ color: C.sub }}>{routedTraceId && source === "live"
              ? "The requested trace is unavailable in this tenant."
              : "Select a trace to inspect its metadata and any captured content or judge verdicts."}</div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function TraceSpans({ traceId, root }) {
  const [state, setState] = useState({ spans: [], truncated: false, loading: true, error: null });
  const [selected, setSelected] = useState(null);
  useEffect(() => {
    let active = true;
    setState({ spans: [], truncated: false, loading: true, error: null });
    setSelected(null);
    fetch(`${root}/api/setup/token`, { credentials: "same-origin" })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("Span access unavailable")))
      .then((auth) => fetch(`${root}/api/explain/spans/${encodeURIComponent(traceId)}`, {
        credentials: "same-origin", headers: { "X-Verdict-Setup": auth.setupToken },
      }))
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("Span list unavailable")))
      .then((body) => { if (active) setState({ spans: body.spans || [], truncated: Boolean(body.truncated), loading: false, error: null }); })
      .catch((error) => { if (active) setState({ spans: [], truncated: false, loading: false, error: String(error) }); });
    return () => { active = false; };
  }, [root, traceId]);
  return <div className="border p-3 text-xs" style={{ borderColor: C.border }}>
    <strong>Recorded spans</strong>
    {state.loading ? <p className="mt-2" style={{ color: C.sub }}>Loading spans…</p>
      : state.error ? <p role="alert" className="mt-2" style={{ color: C.amber }}>{state.error}</p>
        : state.spans.length === 0 ? <p className="mt-2" style={{ color: C.sub }}>No tenant-owned spans recorded for this call.</p>
          : <><div className="mt-2 space-y-1">{state.spans.map((span) => <button type="button"
            key={span.spanId} onClick={() => setSelected(span)}
            className="block text-left w-full border p-2" style={{ borderColor: selected?.spanId === span.spanId ? C.green : C.border }}>
            <strong>{span.name || "Unnamed span"}</strong> · {span.durationMs == null ? "duration unknown" : `${span.durationMs} ms`}
            {span.error && <span style={{ color: C.red }}> · {span.error}</span>}
          </button>)}</div>
            {state.truncated && <p className="mt-2" style={{ color: C.amber }}>Showing the newest 100 tenant-owned spans.</p>}
            {selected && <EvidenceExplain key={selected.spanId} root={root} scope="span"
              traceId={traceId} spanId={selected.spanId} />}</>}
  </div>;
}

function TraceDetail({ s, onClose, onEvaluate = null, source = "sample" }) {
  const provider = providerPresentation(s.provider, s.request_model);
  const recorded = traceTime(s);
  const hasContent = capturedContent(s);
  const hasPrompt = s.prompt_redacted != null;
  const hasResponse = s.response_redacted != null;
  const traceErrorRecorded = s.providerStatus === "provider_error" || s.error != null;
  const toolOnlyResponse = !hasResponse && hasPrompt && !traceErrorRecorded && s.finish_reason === "tool_use";
  const contentLabel = hasPrompt && hasResponse
    ? "Content captured"
    : hasContent
      ? "Content partially captured"
      : "Historical metadata-only trace";
  const judgeStatus = s.judgeStatus || "not_judged";
  const judgeLabel = judgeStatus === "judge_error" ? "Judge error"
    : judgeStatus === "not_judged" ? "Not evaluated"
      : `Judge ${judgeStatus}`;
  const facts = s.deterministicFacts || null;
  const evidenceReason = {
    provider_call_failed: "Provider call failed",
    trace_unfinished: "Call unfinished; response may be partial",
    prompt_not_captured: "Prompt evidence missing",
    response_not_captured: toolOnlyResponse ? "No assistant text (tool use)" : "Response evidence missing",
  }[facts?.notEvaluableReason] || facts?.notEvaluableReason;
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
          <Pill color={C.sub}>{s.cluster_label || s.cluster_id || "Unassigned"}</Pill>
          <Pill color={hasContent ? C.green : C.amber} bg={hasContent ? C.greenBg : C.amberBg}>{contentLabel}</Pill>
          <Pill color={s.traceState === "unfinished" ? C.amber : traceErrorRecorded ? C.red : C.faint} bg={s.traceState === "unfinished" ? C.amberBg : traceErrorRecorded ? C.redBg : undefined}>{s.traceState === "unfinished" ? "Unfinished" : traceErrorRecorded ? "Provider error recorded" : "No provider error recorded"}</Pill>
          <Pill color={judgeStatus === "judge_error" ? C.red : s.judgment ? C.blue : C.faint}>{judgeLabel}</Pill>
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
            {s.response_redacted == null ? (toolOnlyResponse ? "This model call returned tool calls without assistant text." : hasContent ? "Response was not captured for this trace." : "Not available for this historical metadata-only trace.") : (s.response_redacted || "Captured response was empty.")}
          </div>
        </div>
        {s.error && (
          <div className="text-sm p-2.5" style={{ background: C.redBg, color: C.red, border: `1px solid ${C.red}`, borderRadius: 3 }}>
            Provider failure: {s.error}
          </div>
        )}
        <div className="p-3" style={{ background: C.panel2, border: `1px solid ${C.border}`, borderRadius: 3 }}>
          <div className="text-xs font-mono" style={{ color: C.accent2 }}>JUDGE-FREE TRACE FACTS</div>
          <div className="text-xs mt-1" style={{ color: C.faint }}>Literal evidence and structural checks only; these do not determine correctness, sentiment, or response quality.</div>
          {facts ? <div className="grid grid-cols-2 gap-x-3 gap-y-2 mt-3">
            <TraceFact label="Provider outcome" value={s.traceState === "unfinished" ? "Unfinished; no provider error recorded" : facts.providerOutcome === "failed" ? "Provider error recorded" : "No provider error recorded"} />
            <TraceFact label="Judge evidence" value={facts.judgeEligible ? "Eligible" : evidenceReason || "Not evaluable"} />
            <TraceFact label="Prompt evidence" value={facts.promptPresent ? "Present" : "Missing"} />
            <TraceFact label="Response evidence" value={facts.responsePresent ? "Present" : toolOnlyResponse ? "No assistant text (tool use)" : "Missing"} />
            <TraceFact label="Response length" value={facts.responseCharacters == null ? "Unavailable" : `${facts.responseCharacters} characters`} />
            <TraceFact label="Valid JSON" value={factFlag(facts.validJson)} />
            <TraceFact label="Refusal signature" value={factFlag(facts.refusalSignature, "Detected", "Not detected")} />
            <TraceFact label="Apology start" value={factFlag(facts.apologyStart, "Detected", "Not detected")} />
            <TraceFact label="Hedge phrases" value={facts.hedgePhrases == null ? "Unavailable" : facts.hedgePhrases} />
            <TraceFact label="Operation" value={s.operation || "Unavailable"} />
            <TraceFact label="Finish reason" value={s.finish_reason || "Unavailable"} />
            <TraceFact label="Tokens" value={`${s.input_tokens ?? "—"} in / ${s.output_tokens ?? "—"} out`} />
            <TraceFact label="Latency" value={s.latency_ms == null ? "Unavailable" : `${s.latency_ms} ms`} />
            <TraceFact label="Supplied cost" value={s.cost_usd == null ? "Unavailable" : usd(s.cost_usd)} />
          </div> : <div className="text-xs mt-3" style={{ color: C.amber }}>Deterministic facts were not included by this Verdict server. Refresh after upgrading the server.</div>}
        </div>
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
            {judgeStatus === "judge_error" ? "Judge error: the selected evaluator attempted this trace but failed; no PASS/FAIL result is inferred." : "No evaluation result: this trace has not been evaluated by the selected evaluator."}
          </div>
        )}
        <div className="text-xs font-mono pt-1" style={{ color: C.faint }}>trace {s.trace_id.slice(0, 12)}</div>
        {onEvaluate && <button type="button" onClick={onEvaluate} className="border px-3 py-2 text-sm w-full" style={{ borderColor: C.green, color: C.green }}>Evaluate this model call</button>}
        {source === "live" && <EvidenceExplain root={mountedConfigUrl().replace(/\/api\/config$/, "")} scope="trace" traceId={s.trace_id} />}
        {source === "live" && <TraceSpans traceId={s.trace_id} root={mountedConfigUrl().replace(/\/api\/config$/, "")} />}
      </div>
    </Panel>
  );
}

function factFlag(value, yes = "Yes", no = "No") {
  return value == null ? "Unavailable" : value ? yes : no;
}

function TraceFact({ label, value }) {
  return <div className="min-w-0"><div className="text-xs" style={{ color: C.faint }}>{label}</div><div className="text-xs mt-0.5 break-words" style={{ color: C.text }}>{value}</div></div>;
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
    const evaluatorSelectionNeeded = ["selection_required", "invalid_selection"]
      .includes(DATA.evaluation?.status);
    return (
      <div className="space-y-5">
        <div className="text-sm" style={{ color: C.sub }}>
          Judged responses are scored on configured rubric dimensions. Some judges include explanations; Jev returns labels only.
        </div>
        <Panel className="p-5">
          <div className="flex items-start gap-3">
            <Scale size={18} style={{ color: C.amber, marginTop: 1 }} />
            <div className="flex-1">
              <div className="font-semibold">{evaluatorSelectionNeeded ? "Select a Trace evaluator to view judge results" : "No Trace evaluator results have been stored yet"}</div>
              <div className="text-sm mt-1" style={{ color: C.sub }}>
                {evaluatorSelectionNeeded
                  ? "Choose an evaluator identity above to load its persisted judgments."
                  : "Judge charts appear after Evaluator Lab stores results for an eligible trace set."}
              </div>
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
        Judged responses are scored on configured rubric dimensions. Some judges include explanations; Jev returns labels only.
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
  const codexRuns = (DATA.meta.agentRunSources || []).find((item) => item.sourceKind === "codex")?.runs || 0;
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
  return (
    <div className="space-y-5">
      <div className="text-sm" style={{ color: C.sub }}>
        {source === "sample"
          ? "Bundled synthetic sample: the same prompt set is shown across three providers to demonstrate comparison views. Connect live data before drawing provider conclusions."
          : "Unmatched traffic · descriptive only. These provider/model rows summarize the calls each provider actually received; they are not a controlled head-to-head comparison."}
      </div>
      {source === "live" && codexRuns > 0 && <Panel className="p-4"><div className="text-sm" style={{ color: C.sub }}><span style={{ color: C.text }}>{codexRuns.toLocaleString()} Codex agent runs were captured.</span> When Codex completion diagnostics are available, their metadata-only model calls appear below with token usage where a matching session event exists, but without prompt, response, latency, or cost data. Agent-run and model-call counts can differ.</div></Panel>}

      <div className="grid md:grid-cols-3 gap-4">
        {provs.map((p) => (
            <Panel key={p.key} className="p-5">
              <div className="flex items-center gap-2 mb-3">
                <Dot color={p.presentation.color} />
                <span className="font-semibold">{p.label}</span>
              </div>
              <div className="grid grid-cols-2 gap-y-3 gap-x-2 text-sm">
                <Metric label="Pass rate" value={pct(p.passRate)} color={p.passRate != null && p.passRate >= 95 ? C.green : C.red} />
                <Metric label="Avg latency" value={formatLatency(p.avgLatency)} />
                <Metric label="Error rate" value={`${p.errorRate}%`} color={p.errorRate > 5 ? C.amber : C.text} />
                <Metric label="Output tokens" value={k(p.outTok)} />
                <Metric label={p.costUnknown ? "Total cost (partial)" : "Total cost"} value={usd(p.cost)} color={p.costUnknown ? C.amber : C.green} />
                <Metric label="Traces" value={p.n.toLocaleString()} />
              </div>
            </Panel>
        ))}
      </div>

      <div className="grid md:grid-cols-3 gap-5">
        <MiniBar title="Total captured cost" data={costData} fmt={usd} />
        <MiniBar title="Mean latency" data={latData} fmt={formatLatency} />
        <MiniBar title="Output tokens (verbosity)" data={tokData} fmt={k} />
      </div>

      <Panel className="p-5">
        <div className="font-semibold text-sm mb-2 flex items-center gap-2"><BarChart3 size={16} style={{ color: C.accent2 }} /> How to read this view</div>
        <ul className="space-y-2 text-sm" style={{ color: C.sub }}>
          <li className="flex gap-2"><span style={{ color: C.amber }}>•</span> <span><span style={{ color: C.text }}>{source === "sample" ? "Compare cost and latency on identical traffic" : "Use these rows to find operational differences"}</span>{source === "sample" ? " before changing providers or prompts." : "; use a matched replay before attributing a difference to the provider."}</span></li>
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
export function DashboardRoot() {
  const data = useDashboardData();
  const { operationsUrl, operationsPageUrl } = useOperationsConfig();
  return (
    <div style={{ fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif", minHeight: "100%", background: C.bg }}>
      <Dashboard data={data.snapshot} onExit={() => { window.location.href = "/"; }} source={data.source}
        onReload={data.reload} onEvaluatorChange={data.load} onTracePageChange={data.loadTracePage}
        onTraceFilterChange={data.loadTraceFilter}
        traceOffset={data.traceOffset} loadedTraceFilter={{ traceDate: data.traceDate, traceJudgeStatus: data.traceJudgeStatus, traceId: data.traceId }} reloading={data.loading}
        loadError={data.error} operationsUrl={operationsUrl} operationsPageUrl={operationsPageUrl} />
    </div>
  );
}
