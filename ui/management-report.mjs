const MAX_ROWS = 20;

function nonnegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value : null;
}

function whole(value) {
  const number = nonnegative(value);
  return number == null ? null : Math.trunc(number);
}

function boundedText(value, fallback = "Unavailable") {
  return typeof value === "string" && value.trim()
    ? value.trim().slice(0, 256) : fallback;
}

function formatNumber(value) {
  return value.toLocaleString("en-US");
}

function formatPercent(value) {
  return value == null ? "Unavailable" : `${value.toLocaleString("en-US")}%`;
}

function formatMoney(value) {
  return value == null ? "Unavailable" : `$${value.toFixed(4)}`;
}

function formatMs(value) {
  if (value == null) return "Unavailable";
  const digits = value < 10 && !Number.isInteger(value) ? 1 : 0;
  return `${value.toLocaleString("en-US", { maximumFractionDigits: digits })} ms`;
}

function shortDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Unknown";
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }).format(date)
    : "Unknown";
}

function readableDate(value, includeTime = false) {
  if (typeof value !== "string" || !value) return "Unavailable";
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
  if (!Number.isFinite(date.getTime())) return "Unavailable";
  return new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", year: "numeric",
    ...(includeTime ? { hour: "numeric", minute: "2-digit" } : { timeZone: "UTC" }),
  }).format(date);
}

function normalizeMetric(row) {
  const calls = whole(row?.calls);
  if (calls == null) return null;
  const successfulCalls = Math.min(calls, whole(row?.successfulCalls) ?? 0);
  const failedCalls = Math.min(calls - successfulCalls, whole(row?.failedCalls) ?? 0);
  const tokenKnownCalls = Math.min(calls, whole(row?.tokenKnownCalls) ?? 0);
  const costKnownCalls = Math.min(calls, whole(row?.costKnownCalls) ?? 0);
  const latencyKnownCalls = Math.min(calls, whole(row?.latencyKnownCalls) ?? 0);
  return {
    calls, successfulCalls, failedCalls,
    successRatePct: nonnegative(row?.successRatePct),
    inputTokens: whole(row?.inputTokens) ?? 0,
    outputTokens: whole(row?.outputTokens) ?? 0,
    totalTokens: whole(row?.totalTokens) ?? 0,
    tokenKnownCalls,
    costUsd: nonnegative(row?.costUsd), costKnownCalls,
    averageLatencyMs: nonnegative(row?.averageLatencyMs), latencyKnownCalls,
  };
}

function normalizeRows(source, kind) {
  return (Array.isArray(source?.rows) ? source.rows : []).slice(0, MAX_ROWS).flatMap((row) => {
    const metric = normalizeMetric(row);
    if (!metric) return [];
    if (kind === "application") return [{
      name: boundedText(row?.name, "Unattributed"),
      environment: boundedText(row?.environment, "Unspecified"),
      attributed: row?.attributed === true,
      ...metric,
    }];
    return [{
      provider: boundedText(row?.provider, "Unknown provider"),
      model: boundedText(row?.model, "Unknown model"),
      ...metric,
    }];
  });
}

function monitorState(monitor) {
  const current = monitor?.active;
  if (!current) return monitor?.candidate
    ? "Historical preview ready; no active monitor" : "No active monitor";
  const snapshot = current.snapshot;
  if (snapshot?.manifest?.prospective_open) {
    const present = whole(snapshot.manifest.current_unit_ids?.length) ?? 0;
    const target = whole(current.policy?.prospective_target);
    return `Collecting prospective evidence${target ? ` (${present}/${target})` : ""}`;
  }
  const metrics = Array.isArray(snapshot?.comparison?.metrics)
    ? snapshot.comparison.metrics : [];
  const alerts = metrics.filter((metric) => metric?.alert === true).length;
  if (snapshot?.comparison?.status === "alert") {
    return `${alerts || 1} active monitor alert${alerts === 1 ? "" : "s"}`;
  }
  if (snapshot?.comparison?.status === "no_alert") return "No alert in latest comparison";
  return "Active monitor has insufficient evidence";
}

function qualityStatus(data, scope) {
  const evaluation = data?.evaluation || {};
  const coverage = data?.coverage?.evaluation;
  const reportJudged = whole(scope?.judgedCalls);
  const reportCalls = whole(scope?.calls);
  const driftSignals = Array.isArray(data?.driftSignals) ? data.driftSignals : [];
  const availableSignals = Math.max(
    driftSignals.length,
    whole(data?.truncation?.resources?.driftSignals?.available) ?? 0,
  );
  return {
    evaluator: evaluation.status === "selected"
      ? boundedText(evaluation.selectedIdentity?.label || evaluation.selectedId)
      : evaluation.status === "empty" ? "No evaluator evidence" : "Evaluator selection required",
    evaluationCoverage: reportCalls == null && whole(coverage?.traces) == null
      ? "Unavailable"
      : `${formatNumber(reportJudged ?? whole(coverage?.judged) ?? 0)} of ${formatNumber(reportCalls ?? whole(coverage?.traces) ?? 0)} application calls judged`,
    monitor: monitorState(data?.monitor),
    deterministicAnalysis: data?.coverage?.deterministicAnalysis?.status === "completed"
      ? `${data.coverage.deterministicAnalysis.complete ? "Complete" : "Partial"} snapshot`
      : "No completed deterministic analysis snapshot",
    legacyChange: data?.driftAnalysis?.runStatus === "completed_with_signals"
      ? `${formatNumber(availableSignals)} signal${availableSignals === 1 ? "" : "s"}`
      : data?.driftAnalysis?.runStatus === "completed_no_signals"
        ? "No signals in latest legacy comparison" : "No completed legacy comparison",
    availableSignals,
  };
}

export function buildManagementReport(data = {}, { source = "live", generatedAt } = {}) {
  const raw = data?.managementReport;
  const scope = raw?.schema === "management-report-v1" ? normalizeMetric(raw.scope) : null;
  const valid = scope != null;
  const calls = valid ? scope.calls : 0;
  const rawScope = valid ? raw.scope : {};
  const sampledLatency = Math.min(scope?.latencyKnownCalls ?? 0,
    whole(rawScope.latencySampledCalls) ?? 0);
  const identifiedApplications = valid ? whole(rawScope.identifiedApplications) : null;
  const unattributedCalls = valid ? Math.min(calls, whole(rawScope.unattributedCalls) ?? 0) : 0;
  const p50LatencyMs = valid ? nonnegative(rawScope.p50LatencyMs) : null;
  const p95LatencyMs = valid ? nonnegative(rawScope.p95LatencyMs) : null;
  const timelineRows = valid
    ? (Array.isArray(raw.timeline?.rows) ? raw.timeline.rows : []).slice(-31).flatMap((row) => {
      const pointCalls = whole(row?.calls);
      return pointCalls == null || shortDate(row?.date) === "Unknown" ? [] : [{
        date: row.date,
        label: shortDate(row.date),
        calls: pointCalls,
        totalTokens: whole(row?.totalTokens) ?? 0,
        tokenKnownCalls: Math.min(pointCalls, whole(row?.tokenKnownCalls) ?? 0),
      }];
    }) : [];
  const availableDates = Math.max(timelineRows.length,
    whole(raw?.timeline?.availableDates) ?? 0);
  const quality = qualityStatus(data, rawScope);
  const applications = valid ? normalizeRows(raw.applications, "application") : [];
  const models = valid ? normalizeRows(raw.models, "model") : [];
  const applicationAvailable = Math.max(applications.length,
    whole(raw?.applications?.availableRows) ?? 0);
  const modelAvailable = Math.max(models.length, whole(raw?.models?.availableRows) ?? 0);
  const first = readableDate(rawScope.firstCapturedAt, true);
  const latest = readableDate(rawScope.latestCapturedAt, true);
  const period = {
    days: [0, 7, 30, 90].includes(raw?.period?.days) ? raw.period.days : 30,
    startDate: boundedText(raw?.period?.startDate, ""),
    endDate: boundedText(raw?.period?.endDate, ""),
  };
  const attention = [];
  if (scope?.failedCalls) attention.push(`${formatNumber(scope.failedCalls)} failed application call${scope.failedCalls === 1 ? "" : "s"}.`);
  if (unattributedCalls) attention.push(`${formatNumber(unattributedCalls)} application call${unattributedCalls === 1 ? " is" : "s are"} missing service identity.`);
  if (calls && scope?.tokenKnownCalls < calls) attention.push(`Token totals are partial (${formatNumber(scope.tokenKnownCalls)} of ${formatNumber(calls)} calls complete).`);
  if (calls && scope?.costKnownCalls < calls) attention.push(`Cost is partial (${formatNumber(scope.costKnownCalls)} of ${formatNumber(calls)} calls priced).`);
  const activeMetrics = data?.monitor?.active?.snapshot?.comparison?.metrics;
  const activeAlerts = (Array.isArray(activeMetrics) ? activeMetrics : [])
    .filter((item) => item?.alert === true).length;
  if (activeAlerts) attention.push(`${activeAlerts} active monitor alert${activeAlerts === 1 ? "" : "s"}.`);
  if (calls && ["selection_required", "invalid_selection"].includes(data?.evaluation?.status)) attention.push("Select one evaluator before interpreting quality evidence.");
  const judgeErrors = whole(rawScope.judgeErrorCalls)
    ?? whole(data?.coverage?.evaluation?.judgeErrors) ?? 0;
  if (calls && judgeErrors) attention.push(`${judgeErrors} evaluator call${judgeErrors === 1 ? "" : "s"} ended in error.`);
  if (calls && data?.driftAnalysis?.runStatus === "completed_with_signals" && quality.availableSignals) attention.push(`${quality.availableSignals} signal${quality.availableSignals === 1 ? "" : "s"} in legacy fixed-window history.`);
  if (calls && data?.coverage?.deterministicAnalysis?.status !== "completed") attention.push("No completed deterministic analysis snapshot is available.");
  const generated = readableDate(generatedAt || new Date().toISOString(), true);
  const periodRange = period.startDate && period.endDate
    ? `${readableDate(period.startDate)} – ${readableDate(period.endDate)}` : null;
  return {
    title: "Verdict LLM Performance & Utilization Report",
    generatedAt: generated,
    period,
    source: source === "sample" ? "Synthetic sample" : source === "live"
      ? "Live Verdict store" : "Waiting for live store",
    range: `${periodRange || (calls
      ? `${readableDate(rawScope.firstCapturedAt)} – ${readableDate(rawScope.latestCapturedAt)}`
      : "No application telemetry captured")} · UTC`,
    kpis: [
      { label: "Application LLM requests", value: valid ? formatNumber(calls) : "No data", note: "Verdict judge calls excluded" },
      { label: "Total token consumption", value: valid && (!calls || scope.tokenKnownCalls) ? formatNumber(scope.totalTokens) : "Unavailable", note: calls && scope.tokenKnownCalls === calls ? "Complete token coverage" : calls ? `${formatNumber(scope?.tokenKnownCalls ?? 0)} of ${formatNumber(calls)} calls complete` : valid ? "No calls captured" : "No token evidence" },
      { label: "Identified applications", value: valid && identifiedApplications != null ? formatNumber(identifiedApplications) : "Unavailable", note: unattributedCalls ? `${formatNumber(unattributedCalls)} calls unattributed` : calls ? "All calls attributed" : valid ? "No calls captured" : "No application evidence" },
      { label: "Latency profile", value: calls && p50LatencyMs != null ? formatMs(p50LatencyMs) : "Unavailable", note: p50LatencyMs != null ? `p95 ${formatMs(p95LatencyMs)} · newest ${formatNumber(sampledLatency)} of ${formatNumber(scope.latencyKnownCalls)} known latencies` : calls ? "Latency not captured" : "No latency evidence" },
    ],
    timeline: {
      rows: timelineRows,
      scope: availableDates > timelineRows.length
        ? `Newest ${timelineRows.length} of ${availableDates} active UTC dates`
        : `${timelineRows.length} active UTC date${timelineRows.length === 1 ? "" : "s"}`,
    },
    summary: {
      success: calls ? `${formatPercent(scope.successRatePct)} · ${formatNumber(scope.successfulCalls)} of ${formatNumber(calls)} calls` : "Unavailable",
      cost: calls && scope.costKnownCalls
        ? `${formatMoney(scope.costUsd)} · ${scope.costKnownCalls === calls ? "complete" : `partial (${formatNumber(scope.costKnownCalls)} of ${formatNumber(calls)} calls priced)`}`
        : "Unavailable",
      tokenCoverage: calls ? `${formatNumber(scope.tokenKnownCalls)} of ${formatNumber(calls)} calls` : "Unavailable",
      latencyCoverage: calls ? `${formatNumber(scope.latencyKnownCalls)} of ${formatNumber(calls)} calls` : "Unavailable",
      firstCapture: first,
      latestCapture: latest,
    },
    applications: { rows: applications, scope: applicationAvailable > applications.length ? `Top ${applications.length} of ${applicationAvailable} service/environment rows by requests` : `${applications.length} service/environment row${applications.length === 1 ? "" : "s"}` },
    models: { rows: models, scope: modelAvailable > models.length ? `Top ${models.length} of ${modelAvailable} provider/model pairs by requests` : `${models.length} provider/model row${models.length === 1 ? "" : "s"}` },
    quality,
    attention: attention.length ? attention : [calls
      ? "No issues were surfaced by the evidence available to this report."
      : "No application telemetry is available yet."],
  };
}

function csvCell(value) {
  let rendered = value == null ? "" : String(value);
  if (typeof value === "string" && /^[\u0000-\u0020]*[=+\-@]/.test(rendered)) rendered = `'${rendered}`;
  return `"${rendered.replaceAll('"', '""')}"`;
}

export function managementReportCsv(report) {
  const header = ["application", "environment", "attributed", "requests", "successful_requests", "failed_requests", "success_rate_pct", "input_tokens", "output_tokens", "total_tokens", "token_complete_calls", "average_latency_ms", "latency_known_calls", "cost_usd", "cost_known_calls", "period_start", "period_end"];
  const rows = report.applications.rows.map((row) => [
    row.name, row.environment, row.attributed,
    row.calls, row.successfulCalls, row.failedCalls, row.successRatePct, row.inputTokens,
    row.outputTokens, row.totalTokens, row.tokenKnownCalls, row.averageLatencyMs,
    row.latencyKnownCalls, row.costUsd, row.costKnownCalls,
    report.period.startDate, report.period.endDate,
  ]);
  return [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
}

function html(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function htmlTable(rows, kind) {
  const identity = kind === "application" ? "Application / service" : "Model / provider";
  const body = rows.map((row) => {
    const name = kind === "application" ? row.name : row.model;
    const detail = kind === "application"
      ? [row.environment, row.attributed ? "" : "Service identity missing"].filter(Boolean).join(" · ")
      : row.provider;
    return `<tr><td><b>${html(name)}</b><small>${html(detail)}</small></td><td>${formatNumber(row.calls)}</td><td>${html(formatPercent(row.successRatePct))}</td><td>${formatNumber(row.totalTokens)}</td><td>${html(formatMs(row.averageLatencyMs))}</td><td>${html(formatMoney(row.costUsd))}${row.costKnownCalls < row.calls ? '<small>partial</small>' : ""}</td></tr>`;
  }).join("");
  return `<table><thead><tr><th>${identity}</th><th>Requests</th><th>Success</th><th>Tokens</th><th>Avg latency</th><th>Cost</th></tr></thead><tbody>${body || '<tr><td colspan="6">No data available</td></tr>'}</tbody></table>`;
}

export function managementReportHtml(report) {
  const csvUrl = `data:text/csv;charset=utf-8,${encodeURIComponent(managementReportCsv(report))}`;
  const maxCalls = Math.max(1, ...report.timeline.rows.map((point) => point.calls));
  const bars = report.timeline.rows.map((point) => `<div class="bar-cell" title="${html(`${readableDate(point.date)}: ${point.calls} calls, ${point.totalTokens} captured tokens`)}"><b>${formatNumber(point.calls)}</b><div class="bar" style="height:${Math.max(4, Math.round(100 * point.calls / maxCalls))}%"></div><small>${html(point.label)}</small></div>`).join("");
  const cards = report.kpis.map((item) => `<div class="card"><small>${html(item.label)}</small><strong>${html(item.value)}</strong><span>${html(item.note)}</span></div>`).join("");
  const facts = [["Success rate", report.summary.success], ["Estimated cost", report.summary.cost], ["Token coverage", report.summary.tokenCoverage], ["Latency coverage", report.summary.latencyCoverage], ["First capture", report.summary.firstCapture], ["Latest capture", report.summary.latestCapture]].map(([label, value]) => `<div><small>${label}</small><b>${html(value)}</b></div>`).join("");
  const status = [["Evaluator", report.quality.evaluator], ["Evaluation coverage", report.quality.evaluationCoverage], ["Current monitor (as of now)", report.quality.monitor], ["Deterministic analysis (as of now)", report.quality.deterministicAnalysis], ["Legacy change history (as of now)", report.quality.legacyChange]].map(([label, value]) => `<div><small>${label}</small><b>${html(value)}</b></div>`).join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${html(report.title)}</title><style>:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f4f7f5;color:#17211d;font:14px system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:32px}header{display:flex;justify-content:space-between;gap:24px;align-items:start;border-bottom:1px solid #d8e0dd;padding-bottom:22px}.eyebrow,small{color:#687871}h1{margin:4px 0 8px;font-size:28px}.actions a{display:inline-block;background:#176b52;color:white;padding:10px 14px;text-decoration:none}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.card,section{background:white;border:1px solid #d8e0dd;border-radius:8px;padding:18px}.card strong{display:block;font-size:26px;margin:8px 0}.card span{font-size:12px;color:#687871}section{margin:14px 0}h2{font-size:17px;margin:0 0 5px}.chart{height:190px;display:grid;grid-template-columns:36px 1fr;gap:8px;margin-top:18px}.axis{display:flex;flex-direction:column;justify-content:space-between;text-align:right;color:#7b8983;font-size:11px;padding-bottom:24px}.bars{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(28px,1fr);align-items:end;border-bottom:1px solid #cbd6d1;gap:5px;overflow-x:auto}.bar-cell{height:100%;display:grid;grid-template-rows:18px 1fr 22px;align-items:end;text-align:center;font-size:10px}.bar-cell>b{color:#176b52;font-size:10px}.bar{background:#20b486;min-height:4px;border-radius:3px 3px 0 0}.bar-cell small{font-size:9px;white-space:nowrap}.facts{display:grid;grid-template-columns:1fr;gap:11px}.facts div{border-top:1px solid #e2e9e6;padding-top:8px}.facts b{display:block;margin-top:3px}table{border-collapse:collapse;width:100%;margin-top:14px}th,td{text-align:right;border-top:1px solid #e2e9e6;padding:10px}th:first-child,td:first-child{text-align:left}td small{display:block;margin-top:3px}ul{margin-bottom:0;padding-left:20px}li{margin:7px 0}@media(max-width:760px){main{padding:16px}.grid{grid-template-columns:1fr 1fr}header{display:block}.actions{margin-top:16px}section{overflow-x:auto}table{min-width:720px}}@media print{body{background:white}main{padding:0}.actions{display:none}.card,section{break-inside:avoid}}</style></head><body><main><header><div><div class="eyebrow">${html(report.source)}</div><h1>${html(report.title)}</h1><div class="eyebrow">${html(report.range)} · generated ${html(report.generatedAt)}</div></div><div class="actions"><a download="verdict-management-report.csv" href="${csvUrl}">Download CSV</a></div></header><div class="grid">${cards}</div><section><h2>LLM request volume</h2><small>${html(report.timeline.scope)} · application calls only</small><div class="chart"><div class="axis"><span>${formatNumber(maxCalls)}</span><span>${formatNumber(Math.round(maxCalls / 2))}</span><span>0</span></div><div class="bars">${bars || "No application activity"}</div></div></section><section><h2>Report scope</h2><div class="facts">${facts}</div></section><section><h2>Application LLM utilization by environment</h2><small>${html(report.applications.scope)}</small>${htmlTable(report.applications.rows, "application")}</section><section><h2>Model performance and throughput</h2><small>${html(report.models.scope)}</small>${htmlTable(report.models.rows, "model")}</section><section><h2>Verdict status</h2><div class="facts">${status}</div></section><section><h2>Management attention</h2><ul>${report.attention.map((item) => `<li>${html(item)}</li>`).join("")}</ul></section></main></body></html>`;
}
