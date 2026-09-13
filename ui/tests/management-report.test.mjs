import assert from "node:assert/strict";
import test from "node:test";

import {
  buildManagementReport, managementReportCsv, managementReportHtml,
} from "../management-report.mjs";

function metric(calls, failedCalls, inputTokens, outputTokens,
  costUsd, costKnownCalls, averageLatencyMs, latencyKnownCalls) {
  return {
    calls, successfulCalls: calls - failedCalls, failedCalls,
    successRatePct: Math.round(1000 * (calls - failedCalls) / calls) / 10,
    inputTokens, outputTokens, totalTokens: inputTokens + outputTokens,
    tokenKnownCalls: calls, costUsd, costKnownCalls, averageLatencyMs, latencyKnownCalls,
  };
}

function bundle() {
  return {
    managementReport: {
      schema: "management-report-v1",
      period: { days: 30, startDate: "2026-08-13", endDate: "2026-09-11" },
      scope: {
        firstCapturedAt: "2026-09-08T00:00:00Z",
        latestCapturedAt: "2026-09-10T00:03:00Z",
        calls: 28,
        successfulCalls: 27,
        failedCalls: 1,
        successRatePct: 96.4,
        inputTokens: 2800,
        outputTokens: 700,
        totalTokens: 3500,
        tokenKnownCalls: 28,
        costUsd: 0.028,
        costKnownCalls: 27,
        averageLatencyMs: 1000,
        latencyKnownCalls: 26,
        latencySampledCalls: 26,
        p50LatencyMs: 900,
        p95LatencyMs: 1800,
        identifiedApplications: 2,
        unattributedCalls: 0,
        judgedCalls: 20,
        judgeErrorCalls: 1,
      },
      timeline: {
        availableDates: 2,
        shownDates: 2,
        rows: [
          { date: "2026-09-08", calls: 24, totalTokens: 3000, tokenKnownCalls: 24 },
          { date: "2026-09-10", calls: 4, totalTokens: 500, tokenKnownCalls: 4 },
        ],
      },
      applications: {
        availableRows: 2,
        shownRows: 2,
        rows: [{
          name: '=HYPERLINK("https://invalid.test")<img src=x>',
          environment: "production",
          attributed: true,
          ...metric(24, 0, 2400, 600, 0.024, 24, 900, 24),
        }, {
          name: "billing-worker",
          environment: "staging",
          attributed: true,
          ...metric(4, 1, 400, 100, 0.004, 3, 1600, 2),
        }],
      },
      models: {
        availableRows: 2,
        shownRows: 2,
        rows: [{
          provider: "openai", model: "gpt-5-mini",
          ...metric(24, 0, 2400, 600, 0.024, 24, 900, 24),
        }, {
          provider: "custom-provider", model: "custom-model-v1",
          ...metric(4, 1, 400, 100, 0.004, 3, 1600, 2),
        }],
      },
    },
    coverage: {
      evaluation: { traces: 28, judged: 20, judgeErrors: 1, notJudged: 7 },
      deterministicAnalysis: { status: "completed", complete: false,
        completedAt: "2026-09-10T03:00:00Z" },
    },
    evaluation: { status: "selection_required" },
    driftAnalysis: { runStatus: "completed_with_signals" },
    driftSignals: [{ id: "one" }],
    truncation: { resources: { driftSignals: { available: 2, shown: 1 } } },
  };
}

test("report presents one consistent application-only executive summary", () => {
  const report = buildManagementReport(bundle(), {
    source: "live", generatedAt: "2026-09-11T00:00:00Z",
  });

  assert.deepEqual(report.kpis.map((item) => item.value), [
    "28", "3,500", "2", "900 ms",
  ]);
  assert.equal(report.kpis[3].note,
    "p95 1,800 ms · newest 26 of 26 known latencies");
  assert.equal(report.timeline.scope, "2 active UTC dates");
  assert.deepEqual(report.timeline.rows.map(({ date, calls }) => ({ date, calls })), [
    { date: "2026-09-08", calls: 24 },
    { date: "2026-09-10", calls: 4 },
  ]);
  assert.equal(report.summary.success, "96.4% · 27 of 28 calls");
  assert.equal(report.summary.cost, "$0.0280 · partial (27 of 28 calls priced)");
  assert.equal(report.quality.evaluationCoverage, "20 of 28 application calls judged");
  assert.equal(report.range, "Aug 13, 2026 – Sep 11, 2026 · UTC");
  for (const timestamp of [report.generatedAt, report.summary.firstCapture, report.summary.latestCapture]) {
    assert.match(timestamp, /2026/);
    assert.doesNotMatch(timestamp, /T\d|Z$|\+00:00/);
  }
  assert.equal(report.applications.rows.length, 2);
  assert.equal(report.models.rows[1].provider, "custom-provider");
  assert.match(report.attention.join(" "), /1 failed application call/);
  assert.match(report.attention.join(" "), /Select one evaluator/);
  assert.match(report.attention.join(" "), /2 signals in legacy/);
});

test("chart and exports retain dates while excluding sensitive trace fields", () => {
  const report = buildManagementReport(bundle(), {
    generatedAt: "2026-09-11T00:00:00Z",
  });
  const csv = managementReportCsv(report);
  const exportedHtml = managementReportHtml(report);

  assert.match(exportedHtml, /LLM request volume/);
  assert.match(exportedHtml, /Sep 8/);
  assert.match(exportedHtml, />24</);
  assert.match(exportedHtml, /Application LLM utilization by environment/);
  assert.match(exportedHtml, /Model performance and throughput/);
  assert.doesNotMatch(exportedHtml, /simulator|type="range"/i);
  assert.match(exportedHtml,
    /<section><h2>LLM request volume[\s\S]*<\/section><section><h2>Report scope/);
  assert.match(exportedHtml, /&lt;img src=x&gt;/);
  assert.doesNotMatch(exportedHtml, /<img src=x>/);
  assert.match(exportedHtml, /<small>production<\/small>/);
  assert.match(exportedHtml, /<small>staging<\/small>/);
  assert.match(csv, /"'=HYPERLINK/);
  assert.match(csv, /billing-worker/);
  assert.match(csv, /"environment","attributed"/);
  assert.match(csv, /"period_start","period_end"/);
  assert.match(csv, /"2026-08-13","2026-09-11"/);
  assert.doesNotMatch(csv, /prompt|response|trace_id|session_id|user_id/i);
  assert.doesNotMatch(exportedHtml, /prompt_redacted|response_redacted|trace_id|session_id/);
});

test("latency coverage stays distinct from the bounded percentile sample", () => {
  const input = bundle();
  input.managementReport.scope.calls = 100000;
  input.managementReport.scope.latencyKnownCalls = 100000;
  input.managementReport.scope.latencySampledCalls = 10000;
  const report = buildManagementReport(input);

  assert.equal(report.kpis[3].note,
    "p95 1,800 ms · newest 10,000 of 100,000 known latencies");
  assert.equal(report.summary.latencyCoverage, "100,000 of 100,000 calls");
});

test("empty and malformed report evidence remains unavailable instead of zero", () => {
  const empty = buildManagementReport({}, {
    generatedAt: "2026-09-11T00:00:00Z",
  });
  assert.deepEqual(empty.kpis.map((item) => item.value), [
    "No data", "Unavailable", "Unavailable", "Unavailable",
  ]);
  assert.deepEqual(empty.timeline.rows, []);
  assert.deepEqual(empty.attention, ["No application telemetry is available yet."]);

  const observedEmpty = buildManagementReport({ managementReport: {
    schema: "management-report-v1",
    scope: {
      calls: 0, successfulCalls: 0, failedCalls: 0, successRatePct: null,
      inputTokens: 0, outputTokens: 0, totalTokens: 0, tokenKnownCalls: 0,
      costUsd: null, costKnownCalls: 0, averageLatencyMs: null,
      latencyKnownCalls: 0, latencySampledCalls: 0, p50LatencyMs: null,
      p95LatencyMs: null, identifiedApplications: 0, unattributedCalls: 0,
      firstCapturedAt: null, latestCapturedAt: null,
    },
    timeline: { availableDates: 0, shownDates: 0, rows: [] },
    applications: { availableRows: 0, shownRows: 0, rows: [] },
    models: { availableRows: 0, shownRows: 0, rows: [] },
  } });
  assert.deepEqual(observedEmpty.kpis.map((item) => item.value), [
    "0", "0", "0", "Unavailable",
  ]);

  const malformed = bundle();
  malformed.managementReport.scope.calls = -7;
  malformed.managementReport.scope.totalTokens = Number.NaN;
  malformed.managementReport.scope.p50LatencyMs = -1;
  malformed.managementReport.applications.rows[0].calls = 999;
  const defensive = buildManagementReport(malformed);
  assert.equal(defensive.kpis[0].value, "No data");
  assert.equal(defensive.kpis[1].value, "Unavailable");
  assert.equal(defensive.kpis[3].value, "Unavailable");
  assert.deepEqual(defensive.applications.rows, []);
});
